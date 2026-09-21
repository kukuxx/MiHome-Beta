"""MiHome iOS build and HTML plist scanner.

The scanner deliberately separates transport status from parsing status.  A
cache is only advanced from a successfully parsed plist; a search endpoint or
the end of a build window is never treated as a version.
"""

from __future__ import annotations

import asyncio
import html
import json
import plistlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import aiohttp

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - tqdm is only a CLI convenience
    tqdm = None


# ---------------------------------------------------------------------------
# Files and settings
# ---------------------------------------------------------------------------
CACHE_FILE = Path("MiHome-iOS.cache.txt")
XCODE_CACHE_FILE = Path("MiHome-iOS.xcode26-db.cache.txt")
ALL_FILE = Path("MiHome-iOS.all.txt")
LATEST_FILE = Path("MiHome-iOS.latest.txt")
HTML_FILE = Path("MiHome-iOS.html.txt")
HTML_CACHE_FILE = Path("MiHome-iOS.html.cache.json")
HTML_AUDIT_FILE = Path("MiHome-iOS.html.audit.json")

PLIST_BASE = "https://cdn.cnbj1.fds.api.mi-img.com/mijia-ios-adhoc/AppStore/adhoc/plist/"
FEATURE_TEMPLATE = PLIST_BASE + "MiHome-ios-Feature-build{build}.plist"
XCODE_TEMPLATE = PLIST_BASE + "MiHome-ios-xcode26-db-build{build}.plist"
HTML_TEMPLATE = "https://cdn.cnbj1.fds.api.mi-img.com/mijia-ios-adhoc/MiHome{build}.html"

CONCURRENCY = 32
TIMEOUT = 8
RETRIES = 3
RETRY_DELAY = 0.4
BUILD_WINDOW = 1000
REPAIR_WINDOW = 2200
XCODE_INITIAL_BUILD = 2779
HTML_INITIAL_END = 20000
HTML_LOOKAHEAD = 1000
HTML_REPAIR_WINDOW = 1000
HTML_RANGE = "bytes=0-8191"
CACHE_SCHEMA = 7
HTML_SCHEMA = 7
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xml,text/xml,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

VERSION_RE = re.compile(r"\d+(?:\.\d+){1,5}")
BUILD_RE = re.compile(r"/MiHome(\d+)\.html(?:$|[?#])", re.I)
PLIST_RE = re.compile(r"[^\s\"'<>\\]+\.plist(?:\?[^\s\"'<>\\]*)?", re.I)
IPA_URL_RE = re.compile(r"(?:(?:https?:)?//)[^\s\"'<>\\]+?\.ipa(?:\?[^\s\"'<>\\]*)?", re.I)
HTTP_URL_RE = re.compile(r"(?:(?:https?:)?//)[^\s\"'<>\\]+", re.I)


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------
def vkey(version: Any) -> tuple[int, ...]:
    """Return a numeric version tuple; tuple comparison is never lexical."""

    return tuple(int(x) for x in re.findall(r"\d+", str(version)))


def version_value(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list, tuple, bytes)):
        return None
    match = VERSION_RE.search(html.unescape(str(value)).strip())
    return match.group(0) if match else None


def choose_latest(items: Iterable[Optional[Mapping[str, Any]]], previous: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Choose max App Version, then max Build.

    ``previous`` is accepted for compatibility, but only parsed result objects
    should be passed.  A cache metadata row is never sufficient by itself.
    """

    candidates = [dict(item) for item in items if item]
    if previous:
        candidates.append(dict(previous))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (vkey(item.get("version", "")), int(item.get("build", 0))))


def read_int(path: Path, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return default


def write_int(path: Path, value: int) -> None:
    path.write_text(str(int(value)), encoding="utf-8")


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
async def _get_bytes_detailed(session: Any, url: str, range_header: Optional[str] = None) -> tuple[int, bytes, str, Mapping[str, str]]:
    """Fetch bytes and return ``(status, body, category)``.

    Categories are ``ok``, ``not_found``, ``retryable`` or ``http_error``.
    400/404/410 are intentionally never retried; transport failures and the
    known transient statuses are retried with exponential backoff.
    """

    headers = HEADERS.copy()
    if range_header:
        headers["Range"] = range_header
        headers["Accept-Encoding"] = "identity"

    last_status = 0
    for attempt in range(RETRIES):
        try:
            async with session.get(url, headers=headers) as response:
                body = await response.read()
                last_status = response.status
                response_headers = dict(getattr(response, "headers", {}) or {})
                if 200 <= response.status < 300:
                    return response.status, body, "ok", response_headers
                if response.status in {400, 404, 410}:
                    return response.status, b"", "not_found", response_headers
                if response.status not in RETRY_STATUS:
                    return response.status, b"", "http_error", response_headers
        except (aiohttp.ClientError, asyncio.TimeoutError):
            last_status = 0

        if attempt + 1 < RETRIES:
            await asyncio.sleep(RETRY_DELAY * (2**attempt))

    return last_status, b"", "retryable", {}


async def get_bytes(session: Any, url: str, range_header: Optional[str] = None) -> tuple[int, bytes, str]:
    status, body, category, _ = await _get_bytes_detailed(session, url, range_header)
    return status, body, category


# ---------------------------------------------------------------------------
# Plist parser
# ---------------------------------------------------------------------------
VERSION_KEYS = (
    "bundle-version",
    "CFBundleShortVersionString",
    "bundle-short-version-string",
    "app-version",
    "versionName",
    "short-version",
    "version",
    "CFBundleVersion",
)
_VERSION_KEY_PRIORITY = {key.lower().replace("_", "-"): index for index, key in enumerate(VERSION_KEYS)}
_VERSION_NOISE = re.compile(r"(?:sdk|ios|iphone|ipad|platform|deployment|minimum|maximum|xcode|swift|os[-_ ]?version|build)", re.I)


def _normal_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")


def _version_from_mapping(mapping: Mapping[str, Any]) -> tuple[Optional[str], int]:
    """Find an app version in one plist mapping and give it a confidence score."""

    candidates: list[tuple[int, int, str]] = []
    app_context = " ".join(
        str(mapping.get(key, ""))
        for key in ("bundle-identifier", "bundleIdentifier", "title", "name", "display-name", "subtitle")
    ).lower()
    context_bonus = 20 if any(token in app_context for token in ("mihome", "mi home", "xiaomi", "mijia", "米家")) else 0

    for raw_key, raw_value in mapping.items():
        key = _normal_key(raw_key)
        if _VERSION_NOISE.search(key):
            continue
        if key not in _VERSION_KEY_PRIORITY and not any(token in key for token in ("version", "release")):
            continue
        parsed = version_value(raw_value)
        if not parsed:
            continue
        priority = _VERSION_KEY_PRIORITY.get(key, 50)
        score = context_bonus + max(1, 50 - priority)
        if any(token in key for token in ("bundle", "app", "release", "short", "display", "name")):
            score += 5
        candidates.append((priority, -score, parsed))

    if not candidates:
        return None, 0
    # Key priority wins over confidence for the same mapping; version ordering
    # is applied later when multiple plist records are compared.
    priority, negative_score, parsed = min(candidates, key=lambda item: (item[0], item[1]))
    return parsed, -negative_score


def _iter_dicts(root: Any) -> Iterable[dict[str, Any]]:
    stack = [root]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            yield value
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def _assets_to_urls(assets: Any) -> list[str]:
    found: list[str] = []

    def add(value: Any, *, software_package: bool = False) -> None:
        if value is None or isinstance(value, (dict, list, tuple)):
            return
        text = html.unescape(str(value)).strip()
        matches = IPA_URL_RE.findall(text)
        # Apple OTA manifests identify an IPA by asset kind.  Do not require a
        # cosmetic `.ipa` suffix: CDN URLs can be signed or extensionless.
        if software_package and not matches:
            matches = HTTP_URL_RE.findall(text)
        for url in matches:
            found.append(url if not url.startswith("//") else "https:" + url)

    if isinstance(assets, list):
        for item in assets:
            if isinstance(item, dict):
                is_software_package = _normal_key(item.get("kind", "")) == "software-package"
                for key in ("url", "download-url", "downloadUrl", "asset-url", "ipa-url", "software-package"):
                    add(item.get(key), software_package=is_software_package and key in {"url", "download-url", "downloadUrl", "asset-url"})
            else:
                add(item)
    elif isinstance(assets, dict):
        is_software_package = _normal_key(assets.get("kind", "")) == "software-package"
        for key in ("url", "download-url", "downloadUrl", "asset-url", "ipa-url", "software-package"):
            add(assets.get(key), software_package=is_software_package and key in {"url", "download-url", "downloadUrl", "asset-url"})
    else:
        add(assets)
    return list(dict.fromkeys(found))


def _urls_in_mapping(mapping: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    for key, value in mapping.items():
        key_text = _normal_key(key)
        if key_text in {"assets", "asset", "download", "software-package", "software-url", "ipa-url"}:
            urls.extend(_assets_to_urls(value))
        elif key_text.endswith("url") or key_text == "url":
            urls.extend(_assets_to_urls(value))
    return list(dict.fromkeys(urls))


def _all_ipa_urls(root: Any) -> list[str]:
    """Find IPA URLs anywhere in a changed plist layout.

    Vendor plists occasionally move the asset URL under an unrecognised key.
    At this fallback level the URL itself is a stronger signal than that key.
    """

    urls: list[str] = []
    for mapping in _iter_dicts(root):
        urls.extend(_urls_in_mapping(mapping))
        for value in mapping.values():
            if not isinstance(value, (dict, list, tuple)):
                urls.extend(_assets_to_urls(value))
    return list(dict.fromkeys(urls))


def _all_version_values(root: Any) -> list[tuple[tuple[int, ...], int, str]]:
    """Collect app-version candidates without accepting SDK/iOS noise."""

    versions: list[tuple[tuple[int, ...], int, str]] = []
    for mapping in _iter_dicts(root):
        version, score = _version_from_mapping(mapping)
        if version:
            versions.append((vkey(version), score, version))
        for key, value in mapping.items():
            if isinstance(value, (dict, list, tuple, bytes)):
                continue
            text = html.unescape(str(value)).strip()
            if not re.search(r"(?:mihome|mi\s+home|米家)", text, re.I):
                continue
            parsed = version_value(text)
            if parsed:
                versions.append((vkey(parsed), 25, parsed))
    return versions


def _iter_metadata_items(root: Any) -> Iterable[tuple[Mapping[str, Any], Any]]:
    """Yield standard metadata/assets pairs at any nesting level."""

    for mapping in _iter_dicts(root):
        metadata = mapping.get("metadata")
        if isinstance(metadata, dict) and ("assets" in mapping or "assets" in metadata):
            yield metadata, mapping.get("assets", metadata.get("assets"))


def _candidate_score(mapping: Mapping[str, Any], ipa: str) -> int:
    context = " ".join(str(mapping.get(key, "")) for key in ("bundle-identifier", "title", "name", "subtitle")).lower()
    score = 5 if ".ipa" in ipa.lower() else 0
    if any(token in context for token in ("mihome", "mi home", "xiaomi", "mijia", "米家")):
        score += 20
    return score


def parse_plist(data: bytes) -> Optional[tuple[str, str]]:
    """Parse standard OTA plists first, then recursively recover changed layouts."""

    try:
        root = plistlib.loads(data)
    except (plistlib.InvalidFileException, ValueError, TypeError, OverflowError):
        return None

    candidates: list[tuple[tuple[int, ...], int, str, str]] = []

    # First pass: standard OTA metadata/assets pairs.  Bundle identifier is
    # deliberately only a confidence signal, never a hard requirement.
    for metadata, assets in _iter_metadata_items(root):
        version, confidence = _version_from_mapping(metadata)
        for ipa in _assets_to_urls(assets):
            if version:
                candidates.append((vkey(version), confidence + _candidate_score(metadata, ipa), version, ipa))

    # Recursive fallback: arbitrary nesting and alternate URL/version keys.
    for mapping in _iter_dicts(root):
        version, confidence = _version_from_mapping(mapping)
        if not version:
            continue
        for ipa in _urls_in_mapping(mapping):
            candidates.append((vkey(version), confidence + _candidate_score(mapping, ipa), version, ipa))

    if not candidates:
        # Last-resort pairing for layouts where version and URL live in separate
        # sibling structures.  Noise keys are filtered before this point.
        versions = _all_version_values(root)
        urls = _all_ipa_urls(root)
        if versions and urls:
            _, _, version = max(versions, key=lambda item: (item[0], item[1]))
            return version, list(dict.fromkeys(urls))[0]
        return None

    _, _, version, ipa = max(candidates, key=lambda item: (item[0], item[1]))
    return version, ipa


@dataclass
class FetchResult:
    status: str
    parsed: Optional[tuple[str, str]] = None
    http_status: int = 0


async def fetch_plist(session: Any, url: str) -> FetchResult:
    status, data, category = await get_bytes(session, url)
    if category != "ok" or status not in (200, 206):
        return FetchResult(category, http_status=status)
    parsed = parse_plist(data)
    if parsed:
        return FetchResult("ok", parsed, status)
    return FetchResult("parse_failed", http_status=status)


async def scan_plist_build(session: Any, template: str, build: int) -> tuple[Optional[dict[str, Any]], str]:
    url = template.format(build=build)
    result = await fetch_plist(session, url)
    if result.parsed:
        version, ipa = result.parsed
        return {"build": int(build), "version": version, "ipa": ipa, "plist": url}, "ok"
    return None, result.status


# ---------------------------------------------------------------------------
# Generic bounded worker scanner
# ---------------------------------------------------------------------------
Worker = Callable[[int], Awaitable[tuple[Any, ...]]]


async def scan_numbers(
    numbers: Iterable[int],
    worker: Worker,
    label: str = "scan",
    retries: int = 1,
    concurrency: int = CONCURRENCY,
    show_progress: bool = False,
) -> tuple[list[dict[str, Any]], list[int], list[dict[str, Any]]]:
    """Scan a bounded set with a fixed worker pool.

    Only ``retryable`` rows enter later rounds.  404/400 and parse failures are
    recorded but never retried or allowed to stop later build numbers.
    """

    pending = sorted({int(number) for number in numbers})
    results: list[dict[str, Any]] = []
    audit: dict[int, dict[str, Any]] = {}

    async def run_round(values: list[int], round_no: int) -> list[int]:
        if not values:
            return []
        queue: asyncio.Queue[int] = asyncio.Queue()
        for value in values:
            queue.put_nowait(value)
        retryable: set[int] = set()
        bar = tqdm(total=len(values), desc=label if round_no == 0 else f"{label} retry-{round_no}") if show_progress and tqdm else None

        async def worker_loop() -> None:
            while True:
                try:
                    number = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    try:
                        worker_result = await worker(number)
                        if len(worker_result) == 3:
                            result, category, detail = worker_result
                        else:
                            result, category = worker_result
                            detail = None
                        if isinstance(category, bool):
                            category = "retryable" if category else ("ok" if result else "not_found")
                    except Exception as exc:  # one build must never cancel the pool
                        result, category = None, "retryable"
                        detail = None
                        audit.setdefault(number, {"build": number, "attempts": []}).setdefault("errors", []).append(type(exc).__name__)
                    row = audit.setdefault(number, {"build": number, "attempts": []})
                    row.setdefault("attempts", []).append(category)
                    if isinstance(detail, dict):
                        row.update(detail)
                    if result:
                        results.append(result)
                    if category == "retryable":
                        retryable.add(number)
                    if result and "version" in result:
                        row["version"] = result["version"]
                    row["status"] = category
                finally:
                    queue.task_done()
                    if bar:
                        bar.update(1)

        tasks = [asyncio.create_task(worker_loop()) for _ in range(max(1, min(concurrency, len(values))))]
        await asyncio.gather(*tasks)
        if bar:
            bar.close()
        return sorted(retryable)

    for round_no in range(retries + 1):
        pending = await run_round(pending, round_no)
        if not pending:
            break

    return results, pending, [audit[key] for key in sorted(audit)]


# ---------------------------------------------------------------------------
# Cache manager and generic build scanner
# ---------------------------------------------------------------------------
def meta_path(cache_file: Path) -> Path:
    return cache_file.with_name(cache_file.name.replace(".txt", ".meta.json"))


def audit_path(cache_file: Path) -> Path:
    name = cache_file.name
    if name.endswith(".cache.txt"):
        name = name[: -len(".cache.txt")] + ".audit.json"
    else:
        name = name + ".audit.json"
    return cache_file.with_name(name)


def load_meta(path: Path) -> Optional[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return {
            "schema": int(raw["schema"]),
            "build": int(raw["build"]),
            "version": str(raw["version"]),
            "pending_builds": sorted({int(x) for x in raw.get("pending_builds", [])}),
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


@dataclass
class CacheState:
    raw_build: Optional[int]
    meta: Optional[dict[str, Any]]
    needs_recovery: bool


@dataclass
class CacheManager:
    path: Path
    schema: int = CACHE_SCHEMA

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    @property
    def metadata_path(self) -> Path:
        return meta_path(self.path)

    def load(self) -> CacheState:
        raw_build = read_int(self.path)
        meta = load_meta(self.metadata_path)
        recovery = raw_build is not None and (meta is None or meta.get("schema") != self.schema)
        return CacheState(raw_build, meta, recovery)

    def save(self, item: Mapping[str, Any], pending_builds: Iterable[int] = ()) -> None:
        """Persist only a parsed latest result; never persist a window endpoint."""

        build = int(item["build"])
        version = str(item["version"])
        write_int(self.path, build)
        self.metadata_path.write_text(
            json.dumps(
                {
                    "schema": self.schema,
                    "build": build,
                    "version": version,
                    "pending_builds": sorted({int(x) for x in pending_builds}),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


@dataclass(frozen=True)
class BuildScannerConfig:
    name: str
    template: str
    cache_file: Path
    initial_build: int


@dataclass
class BuildScanOutcome:
    found: list[dict[str, Any]]
    pending: list[int]
    latest: Optional[dict[str, Any]]
    audit: list[dict[str, Any]] = field(default_factory=list)


class GenericBuildScanner:
    def __init__(
        self,
        session: Any,
        config: BuildScannerConfig,
        *,
        build_window: int = BUILD_WINDOW,
        repair_window: int = REPAIR_WINDOW,
        retries: int = 2,
        concurrency: int = CONCURRENCY,
        fetcher: Optional[Callable[[int], Awaitable[tuple[Optional[dict[str, Any]], str]]]] = None,
    ) -> None:
        self.session = session
        self.config = config
        self.build_window = max(1, int(build_window))
        self.repair_window = max(0, int(repair_window))
        self.retries = max(0, int(retries))
        self.concurrency = max(1, int(concurrency))
        self.fetcher = fetcher

    async def _scan_one(self, build: int) -> tuple[Optional[dict[str, Any]], str]:
        if self.fetcher:
            return await self.fetcher(build)
        return await scan_plist_build(self.session, self.config.template, build)

    async def scan(self) -> BuildScanOutcome:
        cache = CacheManager(self.config.cache_file)
        state = cache.load()
        numbers: set[int] = set()
        if state.raw_build is None:
            numbers.update(range(self.config.initial_build, self.config.initial_build + self.build_window))
        else:
            # Always inspect behind a cached build.  This is what repairs a
            # schema-valid but incorrect cache that jumped past a newer version.
            start = max(self.config.initial_build, state.raw_build - self.repair_window)
            end = state.raw_build + max(self.repair_window, self.build_window)
            numbers.update(range(start, end + 1))
            if state.meta:
                numbers.update(int(x) for x in state.meta.get("pending_builds", []))

        found, pending, audit = await scan_numbers(
            numbers,
            self._scan_one,
            label=self.config.name,
            retries=self.retries,
            concurrency=self.concurrency,
        )
        latest = choose_latest(found)
        if latest:
            cache.save(latest, pending)
        save_json(
            audit_path(Path(self.config.cache_file)),
            {
                "schema": CACHE_SCHEMA,
                "pipeline": self.config.name,
                "cache_before": state.raw_build,
                "cache_recovery": state.needs_recovery,
                "latest": latest,
                "pending_builds": pending,
                "rows": audit,
            },
        )
        return BuildScanOutcome(found, pending, latest, audit)


async def scan_pipeline(session: Any, cache_file: Path, template: str, initial_build: int, label: str) -> tuple[list[dict[str, Any]], list[int], Optional[int], Optional[str]]:
    """Compatibility wrapper for callers of the original script."""

    result = await GenericBuildScanner(
        session,
        BuildScannerConfig(label, template, cache_file, initial_build),
    ).scan()
    return result.found, result.pending, (result.latest or {}).get("build"), (result.latest or {}).get("version")


# ---------------------------------------------------------------------------
# HTML plist discovery and scanning
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    for _ in range(4):
        new = unquote(html.unescape(text)).replace("\\/", "/")
        if new == text:
            break
        text = new
    return text


def _canonical_url(url: str, base_url: str = PLIST_BASE) -> Optional[str]:
    value = url.strip(" '\")>);,\r\n\t")
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("MiHome-ios-"):
        # Bare plist names in the HTML index belong to the CDN plist folder,
        # not to the directory containing the HTML page.
        value = urljoin(PLIST_BASE, value)
    if value.startswith("/"):
        value = urljoin(base_url, value)
    elif not re.match(r"^https?://", value, re.I):
        value = urljoin(base_url, value)
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc or not parts.path.lower().endswith(".plist"):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def plist_candidates(text: str, base_url: str = PLIST_BASE) -> list[str]:
    text = normalize_text(text)
    raw: list[str] = []
    raw.extend(PLIST_RE.findall(text))
    raw.extend(match.group(1) for match in re.finditer(r"url\s*=\s*([^&\"'<>\s]+)", text, re.I))
    result: list[str] = []
    for value in raw:
        url = _canonical_url(value, base_url)
        if url and url not in result:
            result.append(url)
    return result


VERSION_PATTERNS = (
    re.compile(r"(?:米家|MiHome|Mi\s+Home)\s*[Vv]?\s*(\d+(?:\.\d+){1,5})", re.I),
    re.compile(r"(?:CFBundleShortVersionString|bundle-version|app[-_ ]?version|versionName)\s*[\"']?\s*[:=]\s*[\"']?\s*[Vv]?\s*(\d+(?:\.\d+){1,5})", re.I),
    re.compile(r"\bVersion\s*[:=]\s*[Vv]?\s*(\d+(?:\.\d+){1,5})", re.I),
    re.compile(r"<title[^>]*>[^<]*?[Vv]?\s*(\d+(?:\.\d+){1,5})[^<]*</title>", re.I),
)


def visible_version(text: str) -> Optional[str]:
    text = normalize_text(text)
    for pattern in VERSION_PATTERNS:
        for match in pattern.finditer(text):
            before = text[max(0, match.start() - 24) : match.start()].lower()
            context = text[max(0, match.start() - 48) : min(len(text), match.end() + 48)].lower()
            if re.search(r"(?:ios|sdk|platform|xcode)\s*$", before) or (
                pattern.pattern.startswith("<title") and re.search(r"\b(?:ios|sdk|platform|xcode)\b", context)
            ):
                continue
            return match.group(1)
    return None


class HtmlScanner:
    def __init__(
        self,
        session: Any,
        *,
        template: str = HTML_TEMPLATE,
        initial_end: int = HTML_INITIAL_END,
        lookahead: int = HTML_LOOKAHEAD,
        repair_window: int = HTML_REPAIR_WINDOW,
        retries: int = 2,
        concurrency: int = CONCURRENCY,
    ) -> None:
        self.session = session
        self.template = template
        self.initial_end = initial_end
        self.lookahead = lookahead
        self.repair_window = repair_window
        self.retries = retries
        self.concurrency = concurrency
        self._plist_tasks: dict[str, asyncio.Task[FetchResult]] = {}

    async def _fetch_plist_once(self, url: str) -> FetchResult:
        task = self._plist_tasks.get(url)
        if task is None:
            task = asyncio.create_task(fetch_plist(self.session, url))
            self._plist_tasks[url] = task
        return await task

    async def scan_page(self, build: int) -> tuple[Optional[dict[str, Any]], str, dict[str, Any]]:
        url = self.template.format(build=build)
        status, data, category, range_headers = await _get_bytes_detailed(self.session, url, HTML_RANGE)
        audit: dict[str, Any] = {"build": int(build), "html": url, "status": category, "http_status": status, "candidates": []}
        if category != "ok" or status not in (200, 206):
            return None, category, audit

        text = data.decode("utf-8", "replace")
        candidates = plist_candidates(text, url)
        page_version = visible_version(text)

        # If Content-Range proves that the requested range is truncated, fetch
        # the remainder.  A test double/CDN without Content-Range is treated as
        # complete, preserving the cheap range path when it already contains
        # all useful signals.
        content_range = range_headers.get("Content-Range", "")
        range_complete = status != 206 or not content_range
        range_match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+)", content_range, re.I)
        if range_match:
            range_complete = int(range_match.group(2)) + 1 >= int(range_match.group(3))
        if status == 206 and (not range_complete or (not candidates and not page_version)):
            full_status, full_data, full_category = await get_bytes(self.session, url)
            if full_category != "ok" or full_status not in (200, 206):
                audit.update({"status": full_category, "http_status": full_status})
                return None, full_category, audit
            text = full_data.decode("utf-8", "replace")
            candidates = plist_candidates(text, url)
            page_version = visible_version(text)

        parsed_rows: list[dict[str, Any]] = []
        for plist_url in candidates:
            parsed = await self._fetch_plist_once(plist_url)
            row = {"url": plist_url, "status": parsed.status, "http_status": parsed.http_status}
            if parsed.parsed:
                row["version"] = parsed.parsed[0]
                row["ipa"] = parsed.parsed[1]
                parsed_rows.append({
                    "build": int(build),
                    "version": parsed.parsed[0],
                    "ipa": parsed.parsed[1],
                    "html": url,
                    "plist": plist_url,
                })
            audit["candidates"].append(row)

        if parsed_rows:
            best = choose_latest(parsed_rows)
            audit.update({"status": "ok", "version": best["version"], "parsed_results": parsed_rows})
            return best, "ok", audit
        if page_version:
            result = {"build": int(build), "version": page_version, "html": url, "ipa": None}
            audit.update({"status": "ok", "version": page_version})
            return result, "ok", audit

        audit["status"] = "parse_failed"
        return None, "parse_failed", audit

    async def _worker(self, build: int) -> tuple[Optional[dict[str, Any]], str, dict[str, Any]]:
        return await self.scan_page(build)

    async def scan(self) -> tuple[list[dict[str, Any]], list[int], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        cache = load_json(HTML_CACHE_FILE)
        existing = load_html()
        if not isinstance(cache, dict) or cache.get("scan_version") != HTML_SCHEMA or cache.get("latest_build") is None:
            numbers = range(1, self.initial_end + 1)
        else:
            latest_build = int(cache["latest_build"])
            pending = {int(x) for x in cache.get("pending_builds", [])}
            start = max(1, latest_build - self.repair_window)
            numbers = sorted(pending | set(range(start, latest_build + self.lookahead + 1)))

        found, pending, audit = await scan_numbers(
            numbers,
            self._worker,
            label="HTML",
            retries=self.retries,
            concurrency=self.concurrency,
        )
        for item in found:
            existing[_html_key(item)] = item
        # A page can advertise more than one plist (for example Feature and
        # xcode26-db).  Keep every parsed candidate in HTML output, while the
        # worker result remains the page's highest-version row for compatibility.
        for row in audit:
            for item in row.get("parsed_results", []):
                existing[_html_key(item)] = item

        latest = choose_latest(existing.values()) if existing else None
        if latest:
            save_html_cache(latest["version"], latest["build"], pending)
        save_html(existing)
        save_json(HTML_AUDIT_FILE, {"schema": HTML_SCHEMA, "rows": audit})
        return found, pending, existing, audit


def _html_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("html") or f"{item.get('build')}:{item.get('version')}"),
        str(item.get("version", "")),
        str(item.get("ipa") or ""),
    )


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def save_html_cache(version: str, build: int, pending: Iterable[int]) -> None:
    save_json(
        HTML_CACHE_FILE,
        {
            "scan_version": HTML_SCHEMA,
            "latest_version": version,
            "latest_build": int(build),
            "pending_builds": sorted({int(x) for x in pending}),
        },
    )


def load_html() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    try:
        lines = HTML_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        version, url = parts
        match = BUILD_RE.search(url)
        result[_html_key({"version": version, "build": int(match.group(1)) if match else 0, "html": url, "ipa": None})] = {
            "version": version,
            "build": int(match.group(1)) if match else 0,
            "html": url,
            "ipa": None,
        }
    return result


def save_html(data: Mapping[str, Mapping[str, Any]]) -> None:
    rows = sorted(data.values(), key=lambda item: (vkey(item.get("version", "")), int(item.get("build", 0))), reverse=True)
    HTML_FILE.write_text("".join(f'{row["version"]} {row["html"]}\n' for row in rows), encoding="utf-8")


async def scan_html(session: Any):
    """Compatibility wrapper for the original top-level HTML scanner."""

    return await HtmlScanner(session).scan()


# ---------------------------------------------------------------------------
# Result merger and output writer
# ---------------------------------------------------------------------------
class ResultMerger:
    def __init__(self, all_file: Path = ALL_FILE, latest_file: Path = LATEST_FILE) -> None:
        self.all_file = Path(all_file)
        self.latest_file = Path(latest_file)
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def load_existing(self) -> None:
        try:
            lines = self.all_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                version, ipa = parts
                self.rows[(version, ipa)] = {"version": version, "ipa": ipa, "build": 0, "source": "old"}

    def add(self, items: Iterable[Mapping[str, Any]]) -> None:
        for item in items:
            version = version_value(item.get("version"))
            ipa = item.get("ipa")
            if not version or not ipa:
                continue
            key = (version, str(ipa))
            prior = self.rows.get(key, {})
            self.rows[key] = {**prior, **dict(item), "version": version, "ipa": str(ipa)}

    def save(self) -> list[dict[str, Any]]:
        rows = sorted(self.rows.values(), key=lambda item: (vkey(item["version"]), int(item.get("build", 0)), item["ipa"]), reverse=True)
        self.all_file.write_text("".join(f'{row["version"]} {row["ipa"]}\n' for row in rows), encoding="utf-8")
        if rows:
            self.latest_file.write_text(rows[0]["ipa"], encoding="utf-8")
        return rows


def load_ipa() -> dict[tuple[str, str], tuple[str, str]]:
    """Compatibility loader for the original ``(version, ipa)`` map."""

    result: dict[tuple[str, str], tuple[str, str]] = {}
    try:
        lines = ALL_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            result[(parts[0], parts[1])] = (parts[0], parts[1])
    return result


def save_ipa(data: Mapping[Any, Any]) -> None:
    """Compatibility writer backed by :class:`ResultMerger`."""

    merger = ResultMerger()
    merger.load_existing()
    items = []
    for value in data.values():
        if isinstance(value, Mapping):
            items.append(value)
        elif isinstance(value, (tuple, list)) and len(value) >= 2:
            items.append({"version": value[0], "ipa": value[1], "build": 0})
    merger.add(items)
    merger.save()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def main() -> None:
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY,
        limit_per_host=CONCURRENCY,
        ttl_dns_cache=300,
        keepalive_timeout=30,
        enable_cleanup_closed=True,
    )
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, auto_decompress=True) as session:
        feature_scanner = GenericBuildScanner(
            session,
            BuildScannerConfig("Feature", FEATURE_TEMPLATE, CACHE_FILE, 1),
        )
        xcode_scanner = GenericBuildScanner(
            session,
            BuildScannerConfig("xcode26-db", XCODE_TEMPLATE, XCODE_CACHE_FILE, XCODE_INITIAL_BUILD),
        )
        html_scanner = HtmlScanner(session)
        feature, xcode, html_result = await asyncio.gather(
            feature_scanner.scan(), xcode_scanner.scan(), html_scanner.scan()
        )

        merger = ResultMerger()
        merger.load_existing()
        merger.add(feature.found)
        merger.add(xcode.found)
        merger.add(html_result[2].values())
        merged = merger.save()

        print(f"Feature: {len(feature.found)} | cache={(feature.latest or {}).get('build')} | version={(feature.latest or {}).get('version')} | pending={len(feature.pending)}")
        print(f"xcode26-db: {len(xcode.found)} | cache={(xcode.latest or {}).get('build')} | version={(xcode.latest or {}).get('version')} | pending={len(xcode.pending)}")
        print(f"HTML: new={len(html_result[0])} total={len(html_result[2])} pending={len(html_result[1])}")
        print(f"IPA total: {len(merged)}")


if __name__ == "__main__":
    asyncio.run(main())
