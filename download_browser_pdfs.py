from __future__ import annotations

import csv
import json
import os
import select
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent

PMID_COLUMN = "pmid"
DOI_COLUMN = "doi"
IS_FETCH_TARGET_COLUMN = "is_fetch_target"
FETCH_STATUS_COLUMN = "fetch_status"
FETCH_SOURCE_COLUMN = "fetch_source"
FETCH_ERROR_COLUMN = "fetch_error"
DOWNLOAD_STARTED_AT_COLUMN = "download_started_at"
DOWNLOAD_FINISHED_AT_COLUMN = "download_finished_at"
DOWNLOAD_FILENAME_COLUMN = "download_filename"
DOWNLOAD_URL_COLUMN = "download_url"
OUTPUT_PATH_COLUMN = "output_path"
TARGET_ENABLED_VALUE = "Y"

DOWNLOAD_RESULT_FIELDS = [
    FETCH_STATUS_COLUMN,
    FETCH_SOURCE_COLUMN,
    FETCH_ERROR_COLUMN,
    DOWNLOAD_STARTED_AT_COLUMN,
    DOWNLOAD_FINISHED_AT_COLUMN,
    DOWNLOAD_FILENAME_COLUMN,
    DOWNLOAD_URL_COLUMN,
    OUTPUT_PATH_COLUMN,
]


@dataclass(frozen=True)
class PublisherClickRule:
    publisher: str
    pdf_selector: str
    download_selector: str | None = None
    reveal_download_selector: str | None = None
    dismiss_selector: str | None = None
    request_error_selector: str | None = None
    request_error_text: str | None = None


@dataclass(frozen=True)
class FileSnapshot:
    size: int
    modified_ns: int


@dataclass(frozen=True)
class DownloadStrategy:
    priority: int
    automatic: bool
    label: str


class PublisherOpenStatus(Enum):
    OPENED = "opened"
    REQUEST_BLOCKED = "request_blocked"
    AUTOMATION_FAILED = "automation_failed"


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def normalize_cell(value: str | None) -> str:
    return (value or "").strip()


def normalize_pmid(value: str | None) -> str:
    pmid = normalize_cell(value)
    return pmid if pmid.isascii() and pmid.isdecimal() else ""


def normalize_doi(value: str | None) -> str:
    """Normalize common DOI representations while preserving DOI case."""
    cleaned = normalize_cell(value)
    if cleaned.casefold().startswith("doi:"):
        cleaned = cleaned[4:].strip()

    parsed = urlparse(cleaned)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() in {"http", "https"} and host in {
        "doi.org",
        "dx.doi.org",
    }:
        return unquote(parsed.path.lstrip("/")).strip()
    return cleaned


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


def encoded_doi(doi: str) -> str:
    return quote(doi, safe="/:()._-;")


def is_yes(value: str | None) -> bool:
    return normalize_cell(value).upper() == TARGET_ENABLED_VALUE


def ensure_columns(fieldnames: list[str], required_columns: Iterable[str]) -> list[str]:
    merged = list(fieldnames)
    for column in required_columns:
        if column not in merged:
            merged.append(column)
    return merged


def load_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        if len(fieldnames) != len(set(fieldnames)):
            raise ValueError("Input CSV contains duplicate column names")

        rows: list[dict[str, str]] = []
        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"Input CSV has extra values on line {line_number}")
            rows.append({key: value or "" for key, value in row.items()})
        return fieldnames, rows


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, path)


INPUT_CSV = (PROJECT_ROOT / "target_records.csv").resolve()
OUTPUT_CSV = INPUT_CSV
WATCH_DIR = (Path.home() / "Downloads").resolve()
OUTPUT_DIR = (PROJECT_ROOT / "browser").resolve()

BATCH_START = 0
BATCH_LIMIT = 2000
OPEN_DELAY_SECONDS = 0.4
WAIT_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 0.3
MIN_FILE_SIZE_BYTES = 1024
INTERACTIVE_SKIP = True
DRY_RUN = False
OPEN_COMMAND: str | None = None
BATCH_LOG_PATH: Path | None = None
# Matching URLs are recorded as skipped instead of silently disappearing.
BLOCKED_URL_PATTERNS = ["karger.com"]
# Matching URLs remain in the queue but run in the manual-review tier.
MANUAL_URL_PATTERNS = ["link.springer.com"]
AUTOMATE_PUBLISHER_PDF_CLICK = True
PUBLISHER_CLICK_TIMEOUT_SECONDS = 20
AUTO_CLEAR_BROWSER_CACHE = True
CACHE_CLEAR_EVERY_FILES = 12
CACHE_CLEAR_PUBLISHERS = ["ScienceDirect"]
CLEAR_COOKIES_WITH_CACHE = True
DOWNLOAD_BREAK_EVERY_FILES = 2000
DOWNLOAD_BREAK_SECONDS = 60
SCIENCEDIRECT_REQUEST_ERROR_MAX_RETRIES = 3
CLOSE_COMPLETED_AUTOMATIC_TABS = True
CLOSE_SCIENCEDIRECT_TABS_AT_BREAK = True
CHROME_PROFILE_DIRECTORY = "Default"
CHROME_CACHE_ROOT = Path.home() / "Library" / "Caches" / "Google" / "Chrome"
CHROME_USER_DATA_ROOT = (
    Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
)
CHROME_CACHE_DIRECTORIES = (
    CHROME_CACHE_ROOT / CHROME_PROFILE_DIRECTORY / "Cache",
    CHROME_CACHE_ROOT / CHROME_PROFILE_DIRECTORY / "Code Cache",
)
CHROME_PROFILE_ROOT = CHROME_USER_DATA_ROOT / CHROME_PROFILE_DIRECTORY
CHROME_COOKIE_DATABASES = (
    CHROME_PROFILE_ROOT / "Cookies",
    CHROME_PROFILE_ROOT / "Network" / "Cookies",
)
CHROME_COOKIE_FILES = tuple(
    Path(f"{database}{suffix}")
    for database in CHROME_COOKIE_DATABASES
    for suffix in ("", "-journal", "-wal", "-shm")
)

# Lower values run first. Python's stable sort preserves CSV order within each
# publisher group, while all verified automatic routes stay ahead of records
# that need manual review.
AUTO_PUBLISHER_PRIORITY = {
    "ScienceDirect": 0,
    "ACS": 10,
    "Wiley": 20,
    "SAGE": 30,
    "RSC": 40,
    "Taylor & Francis": 50,
    "Oxford Academic": 60,
    "IOPscience": 70,
    "IIAR Journals": 80,
    "AACR Journals": 90,
    "JAMA Network": 100,
}
DIRECT_PDF_PRIORITY = 200
MANUAL_REVIEW_PRIORITY = 1000

# Some institution-provided URLs contain opaque record IDs and cannot be
# derived from a DOI. Keep those exceptional mappings explicit and auditable.
INSTITUTIONAL_URL_OVERRIDES = {
    "10.1007/s13277-015-4345-7": (
        "https://research-ebsco-com.proxy.lib.ohio-state.edu/"
        "c/wpogxq/viewer/pdf/qvffijf37b?route=details"
    ),
}

SCIENCEDIRECT_PROXY_HOST = "www-sciencedirect-com.proxy.lib.ohio-state.edu"
SCIENCEDIRECT_ARTICLE_BASE_URL = (
    f"https://{SCIENCEDIRECT_PROXY_HOST}/science/article/pii"
)
ACS_PROXY_HOST = "pubs-acs-org.proxy.lib.ohio-state.edu"
ACS_ARTICLE_BASE_URL = f"https://{ACS_PROXY_HOST}/doi/full"
WILEY_PROXY_HOST = "onlinelibrary-wiley-com.proxy.lib.ohio-state.edu"
WILEY_ARTICLE_BASE_URL = f"https://{WILEY_PROXY_HOST}/doi/full"
SAGE_PROXY_HOST = "journals-sagepub-com.proxy.lib.ohio-state.edu"
SAGE_ARTICLE_BASE_URL = f"https://{SAGE_PROXY_HOST}/doi/full"
LIEBERT_ARTICLE_BASE_URL = "https://www.liebertpub.com/doi/pdf"
TANDF_PROXY_HOST = "www-tandfonline-com.proxy.lib.ohio-state.edu"
TANDF_ARTICLE_BASE_URL = f"https://{TANDF_PROXY_HOST}/doi/full"

TEMP_SUFFIXES = {".crdownload", ".part", ".download", ".tmp"}

DOI_PREFIX_RULES: list[tuple[str, str]] = [
    ("10.3390/", "https://www.mdpi.com/article/{doi}/pdf"),
    ("10.1007/", "https://link.springer.com/content/pdf/{doi}.pdf"),
    ("10.1038/", "https://link.springer.com/content/pdf/{doi}.pdf"),
    ("10.1002/", f"{WILEY_ARTICLE_BASE_URL}/{{doi}}"),
    ("10.1111/", f"{WILEY_ARTICLE_BASE_URL}/{{doi}}"),
    ("10.1096/", f"{WILEY_ARTICLE_BASE_URL}/{{doi}}"),
    ("10.1021/", f"{ACS_ARTICLE_BASE_URL}/{{doi}}"),
    ("10.3389/", "https://www.frontiersin.org/articles/{doi}/pdf"),
    ("10.1073/", "https://www.pnas.org/doi/pdf/{doi}"),
    ("10.1126/", "https://www.science.org/doi/pdf/{doi}"),
    ("10.1128/", "https://journals.asm.org/doi/pdf/{doi}"),
    ("10.1177/", f"{SAGE_ARTICLE_BASE_URL}/{{doi}}"),
    ("10.1089/", f"{LIEBERT_ARTICLE_BASE_URL}/{{doi}}"),
    ("10.1080/", f"{TANDF_ARTICLE_BASE_URL}/{{doi}}"),
    ("10.1088/", "https://iopscience.iop.org/article/{doi}"),
]


def strip_doi_path_slug(after_doi: str) -> str:
    for slug in ("abs/", "full/", "pdf/", "pdfdirect/", "epdf/", "epub/", "reader/"):
        if after_doi.startswith(slug):
            return after_doi[len(slug):]
    return after_doi


def is_wiley_host(host: str) -> bool:
    return (
        host == "onlinelibrary.wiley.com"
        or host.endswith(".onlinelibrary.wiley.com")
        or host == WILEY_PROXY_HOST
        or host.endswith("-onlinelibrary-wiley-com.proxy.lib.ohio-state.edu")
    )


def is_sage_host(host: str) -> bool:
    return host == "journals.sagepub.com" or host == SAGE_PROXY_HOST


def is_tandfonline_host(host: str) -> bool:
    return (
        host in {"tandfonline.com", "www.tandfonline.com", TANDF_PROXY_HOST}
        or host.endswith(".tandfonline.com")
        or host.endswith("-tandfonline-com.proxy.lib.ohio-state.edu")
    )


def is_public_or_osu_proxy_host(host: str, public_domain: str) -> bool:
    """Match a publisher's public hosts and OSU EZproxy host variants."""
    proxy_host = (
        f"{public_domain.replace('.', '-')}.proxy.lib.ohio-state.edu"
    )
    return (
        host == public_domain
        or host.endswith(f".{public_domain}")
        or host == proxy_host
        or host.endswith(f"-{proxy_host}")
    )


def institutional_url_override(doi: str) -> str:
    normalized = normalize_doi(doi).casefold()
    for configured_doi, url in INSTITUTIONAL_URL_OVERRIDES.items():
        if normalize_doi(configured_doi).casefold() == normalized:
            return url
    return ""


def doi_from_doi_path(path: str) -> str:
    if "/doi/" not in path:
        return ""
    value = strip_doi_path_slug(path.split("/doi/", 1)[1]).strip("/")
    return unquote(value) if value.casefold().startswith("10.") else ""


def rewrite_resolved_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path
    doi = doi_from_doi_path(path)
    quoted_doi = encoded_doi(doi)

    if is_wiley_host(host) and doi:
        return f"{WILEY_ARTICLE_BASE_URL}/{quoted_doi}"

    if host in ("pubs.acs.org", ACS_PROXY_HOST) and doi:
        return f"{ACS_ARTICLE_BASE_URL}/{quoted_doi}"

    if host.endswith("frontiersin.org"):
        if path.endswith("/full"):
            return parsed._replace(
                path=path.removesuffix("/full") + "/pdf",
                query="",
                fragment="",
            ).geturl()
        if "/articles/" in path and not path.endswith("/pdf"):
            return parsed._replace(
                path=path.rstrip("/") + "/pdf",
                query="",
                fragment="",
            ).geturl()

    if host == "pubs.rsc.org" and "/articlelanding/" in path:
        return url.replace("/articlelanding/", "/articlepdf/")

    if host == "elifesciences.org" and "/articles/" in path:
        article_id = path.split("/articles/", 1)[1].split("/", 1)[0]
        if article_id and not article_id.endswith(".pdf"):
            return f"https://elifesciences.org/articles/{article_id}.pdf"

    if host in ("pnas.org", "www.pnas.org") and doi:
        return f"https://www.pnas.org/doi/pdf/{quoted_doi}"

    if host in ("science.org", "www.science.org") and doi:
        return f"https://www.science.org/doi/pdf/{quoted_doi}"

    if host == "journals.asm.org" and doi:
        return f"https://journals.asm.org/doi/pdf/{quoted_doi}"

    if is_sage_host(host) and doi:
        return f"{SAGE_ARTICLE_BASE_URL}/{quoted_doi}"

    if host.endswith("liebertpub.com") and doi:
        return f"{LIEBERT_ARTICLE_BASE_URL}/{quoted_doi}"

    if is_tandfonline_host(host) and doi:
        if "/doi/epdf/" in path or "/doi/pdf/" in path:
            return url
        return f"{TANDF_ARTICLE_BASE_URL}/{quoted_doi}"

    if host == "linkinghub.elsevier.com" and "/retrieve/pii/" in path:
        pii = path.split("/retrieve/pii/", 1)[1].strip("/").split("/", 1)[0]
        if pii:
            return f"{SCIENCEDIRECT_ARTICLE_BASE_URL}/{pii}"

    if host.endswith("sciencedirect.com") and "/article/pii/" in path:
        pii = path.split("/article/pii/", 1)[1].strip("/").split("/", 1)[0]
        if pii:
            return f"{SCIENCEDIRECT_ARTICLE_BASE_URL}/{pii}"

    if host.endswith("link.springer.com"):
        if path.startswith("/content/pdf/") and path.endswith(".pdf"):
            return url
        if path.startswith("/article/"):
            doi_part = path.removeprefix("/article/").strip("/")
            if doi_part:
                return f"https://link.springer.com/content/pdf/{doi_part}.pdf"

    return url


def resolve_best_pdf_url(doi: str, resolve_timeout: float = 8.0) -> str:
    doi = normalize_doi(doi)
    if not doi:
        return ""
    if is_http_url(doi):
        return rewrite_resolved_url(doi)
    if doi.casefold().startswith("10.7554/elife."):
        article_id = doi.rsplit(".", 1)[-1]
        return f"https://elifesciences.org/articles/{article_id}.pdf"

    for prefix, template in DOI_PREFIX_RULES:
        if doi.casefold().startswith(prefix.casefold()):
            return template.format(doi=encoded_doi(doi))

    doi_url = f"https://doi.org/{encoded_doi(doi)}"
    try:
        request = Request(
            doi_url,
            method="HEAD",
            headers={"User-Agent": "Mozilla/5.0 (browser-pdf-fetch/2.0)"},
        )
        with urlopen(request, timeout=resolve_timeout) as response:
            resolved = response.geturl()
        print(f"  [RESOLVE] {doi_url} -> {resolved}")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"  [RESOLVE] failed ({exc}), falling back to doi.org URL")
        return doi_url

    rewritten = rewrite_resolved_url(resolved)
    if rewritten != resolved:
        print(f"  [REWRITE] -> {rewritten}")
    return rewritten


def matching_blocked_pattern(url: str) -> str:
    lowered = url.casefold()
    for pattern in BLOCKED_URL_PATTERNS:
        cleaned = pattern.strip()
        if cleaned and cleaned.casefold() in lowered:
            return cleaned
    return ""


def matching_manual_pattern(url: str) -> str:
    """Return the configured pattern that forces a URL into manual review."""
    lowered = url.casefold()
    for pattern in MANUAL_URL_PATTERNS:
        cleaned = pattern.strip()
        if cleaned and cleaned.casefold() in lowered:
            return cleaned
    return ""


def completed_files(directory: Path) -> list[Path]:
    results: list[Path] = []
    try:
        paths = directory.iterdir()
        for path in paths:
            try:
                if not path.is_file():
                    continue
                if path.suffix.casefold() in TEMP_SUFFIXES:
                    continue
                if any(Path(f"{path}{suffix}").exists() for suffix in TEMP_SUFFIXES):
                    continue
                results.append(path)
            except OSError:
                # Browser downloads can be renamed between discovery and stat calls.
                continue
    except OSError as exc:
        print(f"  [WATCH] unable to scan {directory}: {exc}")
    return results


def file_snapshot(path: Path) -> FileSnapshot | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return FileSnapshot(size=stat.st_size, modified_ns=stat.st_mtime_ns)


def snapshot_completed_files(directory: Path) -> dict[Path, FileSnapshot]:
    snapshot: dict[Path, FileSnapshot] = {}
    for path in completed_files(directory):
        state = file_snapshot(path)
        if state is not None:
            snapshot[path.resolve()] = state
    return snapshot


def is_pdf_file(path: Path) -> bool:
    """Validate PDF content rather than trusting its filename extension."""
    try:
        if path.stat().st_size < MIN_FILE_SIZE_BYTES:
            return False
        with path.open("rb") as handle:
            return b"%PDF-" in handle.read(1024)
    except OSError:
        return False


def read_skip_nonblocking() -> bool:
    if not sys.stdin.isatty():
        return False

    if os.name == "nt":
        # On Windows, select() only accepts sockets, not console input.
        import msvcrt

        if not msvcrt.kbhit():
            return False
        return msvcrt.getwch().lower() == "s"

    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if not ready:
        return False
    return sys.stdin.readline().strip().lower() == "s"


def wait_for_download(
    known: Mapping[Path, FileSnapshot],
) -> tuple[Path | None, bool]:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if INTERACTIVE_SKIP and read_skip_nonblocking():
            return None, True

        candidates: list[tuple[Path, FileSnapshot]] = []
        for path in completed_files(WATCH_DIR):
            resolved_path = path.resolve()
            state = file_snapshot(path)
            if state is None or state == known.get(resolved_path):
                continue
            if is_pdf_file(path):
                candidates.append((path, state))

        if candidates:
            newest, _ = max(candidates, key=lambda item: item[1].modified_ns)
            return newest, False

        time.sleep(POLL_INTERVAL_SECONDS)

    return None, False


def log_event(payload: Mapping[str, object]) -> None:
    if BATCH_LOG_PATH is None:
        return
    BATCH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BATCH_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def chrome_is_running() -> bool:
    if sys.platform != "darwin" or not shutil.which("pgrep"):
        return False
    result = subprocess.run(
        ["pgrep", "-x", "Google Chrome"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def stop_chrome_for_cookie_cleanup(timeout_seconds: float = 15.0) -> tuple[bool, bool]:
    """Quit Chrome so its cookie database can be removed safely."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        print("  [COOKIES] automatic Chrome restart is only supported on macOS")
        return False, False

    was_running = chrome_is_running()
    if not was_running:
        return True, False

    result = subprocess.run(
        ["osascript", "-e", 'tell application "Google Chrome" to quit'],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        error = result.stderr.strip() or f"osascript exited {result.returncode}"
        print(f"  [COOKIES] unable to quit Chrome: {error}")
        return False, True

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not chrome_is_running():
            print("  [COOKIES] Chrome closed for cookie cleanup")
            return True, True
        time.sleep(0.25)

    print("  [COOKIES] Chrome did not close; cookie cleanup was skipped")
    return False, True


def reopen_chrome() -> bool:
    """Reopen Chrome after deleting its cookie database."""
    if sys.platform != "darwin" or not shutil.which("open"):
        return False
    result = subprocess.run(
        ["open", "-a", "Google Chrome"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("  [COOKIES] Chrome reopened; browser logins must be completed again")
        return True
    error = result.stderr.strip() or f"open exited {result.returncode}"
    print(f"  [COOKIES] unable to reopen Chrome: {error}")
    return False


def close_completed_automatic_tab(url: str) -> bool:
    """Close the active automated tab when it still belongs to the target host."""
    expected_host = (urlparse(url).hostname or "").casefold()
    if (
        not expected_host
        or sys.platform != "darwin"
        or not shutil.which("osascript")
    ):
        return False

    apple_script = """
on run argv
    set expectedHost to item 1 of argv
    tell application "Google Chrome"
        if (count of windows) is 0 then return "no_window"
        set currentTab to active tab of front window
        set currentUrl to URL of currentTab
        if currentUrl contains expectedHost then
            close currentTab
            return "closed"
        end if
        return "different_tab"
    end tell
end run
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", apple_script, expected_host],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  [TAB CLOSE] unable to close completed tab: {exc}")
        return False

    if result.stdout.strip() == "closed":
        print(f"  [TAB CLOSE] closed completed automatic tab for {expected_host}")
        return True
    if result.stdout.strip() == "different_tab":
        print("  [TAB CLOSE] active tab changed; left it open for safety")
    return False


def close_sciencedirect_tabs() -> int:
    """Close ScienceDirect tabs while preserving tabs for unrelated sites."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        print("  [TAB CLOSE] automatic ScienceDirect tab cleanup requires macOS")
        return 0

    apple_script = """
on run argv
    set proxyHost to item 1 of argv
    set closedCount to 0
    tell application "Google Chrome"
        repeat with windowIndex from (count of windows) to 1 by -1
            try
                set browserWindow to window windowIndex
                repeat with tabIndex from (count of tabs of browserWindow) to 1 by -1
                    set currentUrl to URL of tab tabIndex of browserWindow
                    if currentUrl contains "sciencedirect.com" or currentUrl contains proxyHost or currentUrl contains "linkinghub.elsevier.com" then
                        close tab tabIndex of browserWindow
                        set closedCount to closedCount + 1
                    end if
                end repeat
            end try
        end repeat
    end tell
    return closedCount
end run
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", apple_script, SCIENCEDIRECT_PROXY_HOST],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  [TAB CLOSE] unable to close ScienceDirect tabs: {exc}")
        return 0

    if result.returncode != 0:
        error = result.stderr.strip() or f"osascript exited {result.returncode}"
        print(f"  [TAB CLOSE] unable to close ScienceDirect tabs: {error}")
        return 0

    try:
        closed_count = int(result.stdout.strip() or "0")
    except ValueError:
        closed_count = 0
    print(f"  [TAB CLOSE] closed {closed_count} remaining ScienceDirect tab(s)")
    return closed_count


def download_break_is_due(successful_downloads: int) -> bool:
    return (
        DOWNLOAD_BREAK_EVERY_FILES > 0
        and successful_downloads > 0
        and successful_downloads % DOWNLOAD_BREAK_EVERY_FILES == 0
    )


def wait_for_scheduled_download_break(successful_downloads: int) -> bool:
    """Pause after each configured group of successful downloads."""
    if not download_break_is_due(successful_downloads):
        return False

    print(
        "\n------------------------------------------------------------\n"
        f"[DOWNLOAD BREAK] {successful_downloads} successful PDFs reached.\n"
        f"Waiting {DOWNLOAD_BREAK_SECONDS} seconds before the next record...\n"
        "------------------------------------------------------------",
        flush=True,
    )
    time.sleep(DOWNLOAD_BREAK_SECONDS)
    print("[DOWNLOAD BREAK COMPLETE] Resuming downloads.\n", flush=True)
    return True


def clear_browser_cache(
    *,
    clear_cookies: bool | None = None,
    restart_chrome: bool = False,
) -> bool:
    """Clear Chrome caches and optionally force cookie cleanup and a restart."""
    found_directory = False
    removed_entries = 0
    removed_cookie_files = 0
    errors: list[str] = []
    should_clear_cookies = (
        CLEAR_COOKIES_WITH_CACHE if clear_cookies is None else clear_cookies
    )

    chrome_stopped = True
    chrome_was_running = False
    if should_clear_cookies:
        chrome_stopped, chrome_was_running = stop_chrome_for_cookie_cleanup()
        if not chrome_stopped:
            errors.append("Chrome could not be stopped; cookies were not cleared")

    for cache_directory in CHROME_CACHE_DIRECTORIES:
        try:
            entries = list(cache_directory.iterdir())
            found_directory = True
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{cache_directory}: {exc}")
            continue

        for entry in entries:
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed_entries += 1
            except FileNotFoundError:
                # Chrome may replace cache entries while cleanup is running.
                continue
            except OSError as exc:
                errors.append(f"{entry}: {exc}")

    if should_clear_cookies and chrome_stopped:
        for cookie_file in CHROME_COOKIE_FILES:
            try:
                cookie_file.unlink()
                removed_cookie_files += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(f"{cookie_file}: {exc}")

    should_reopen_chrome = chrome_was_running or restart_chrome
    if (
        should_clear_cookies
        and chrome_stopped
        and should_reopen_chrome
        and not reopen_chrome()
    ):
        errors.append("Chrome could not be reopened after cookie cleanup")

    cleared_at = now_timestamp()
    if not found_directory:
        print(
            "  [CACHE CLEAR] No Chrome cache directories found for profile "
            f"{CHROME_PROFILE_DIRECTORY!r}.",
            flush=True,
        )
    else:
        print(
            f"  [CACHE CLEAR] Removed {removed_entries} Chrome cache "
            "entry/entries.",
            flush=True,
        )

    if should_clear_cookies:
        if not chrome_stopped:
            print("  [COOKIE CLEAR] SKIPPED because Chrome did not close.", flush=True)
        elif removed_cookie_files:
            print(
                f"  [COOKIE CLEAR] Removed {removed_cookie_files} cookie "
                "database file(s).",
                flush=True,
            )
        else:
            print(
                "  [COOKIE CLEAR] No cookie database files were present; "
                "cookies were already clear.",
                flush=True,
            )

    if errors:
        print(
            f"  [CLEANUP WARNING] Finished with {len(errors)} error(s).",
            flush=True,
        )
        for error in errors[:3]:
            print(f"  [CLEANUP WARNING] {error}", flush=True)

    log_event(
        {
            "event": "browser_cache_cleared",
            "profile": CHROME_PROFILE_DIRECTORY,
            "removed_entries": removed_entries,
            "removed_cookie_files": removed_cookie_files,
            "cookies_requested": should_clear_cookies,
            "restart_requested": restart_chrome,
            "errors": errors,
            "finished_at": cleared_at,
        }
    )
    return not errors


def publisher_uses_scheduled_cleanup(publisher: str) -> bool:
    """Return whether successful downloads from a publisher count toward cleanup."""
    return publisher in CACHE_CLEAR_PUBLISHERS


def maybe_clear_browser_cache(
    successful_downloads: int,
    *,
    publisher: str,
) -> bool:
    """Clear cache at each eligible-publisher successful-download interval."""
    if not AUTO_CLEAR_BROWSER_CACHE or CACHE_CLEAR_EVERY_FILES <= 0:
        return False
    if not publisher_uses_scheduled_cleanup(publisher):
        return False
    if successful_downloads <= 0:
        return False

    completed_in_cycle = successful_downloads % CACHE_CLEAR_EVERY_FILES
    if completed_in_cycle:
        remaining = CACHE_CLEAR_EVERY_FILES - completed_in_cycle
        cleanup_label = "cache/cookie" if CLEAR_COOKIES_WITH_CACHE else "cache"
        print(
            f"  [CLEANUP COUNTDOWN] {successful_downloads} successful "
            f"{publisher} PDF(s); "
            f"next {cleanup_label} cleanup in {remaining} successful PDF(s).",
            flush=True,
        )
        return False

    cleanup_action = (
        "Clearing Chrome cache and cookies now..."
        if CLEAR_COOKIES_WITH_CACHE
        else "Clearing Chrome cache only; cookies will be preserved..."
    )
    print(
        "\n============================================================\n"
        f"[CLEANUP START] {successful_downloads} successful {publisher} "
        "PDFs reached.\n"
        f"{cleanup_action}\n"
        "============================================================",
        flush=True,
    )
    cleared = clear_browser_cache()
    if cleared:
        completion_message = (
            "[CLEANUP COMPLETE] CHROME CACHE AND COOKIES WERE CLEARED.\n"
            "Chrome was reopened; sign in again if required."
            if CLEAR_COOKIES_WITH_CACHE
            else "[CLEANUP COMPLETE] CHROME CACHE WAS CLEARED.\n"
            "Cookies were preserved and Chrome stayed open."
        )
        print(
            "============================================================\n"
            f"{completion_message}\n"
            "============================================================\n",
            flush=True,
        )
    else:
        print(
            "============================================================\n"
            "[CLEANUP WARNING] CLEANUP DID NOT FULLY COMPLETE.\n"
            "Review the warning messages above.\n"
            "============================================================\n",
            flush=True,
        )
    return cleared


def recover_from_sciencedirect_request_error(
    *,
    pmid: str,
    url: str,
    retry_number: int,
) -> bool:
    """Reset Chrome after ScienceDirect rejects a request, ready for a retry."""
    print(
        "\n============================================================\n"
        "[SCIENCEDIRECT REQUEST ERROR]\n"
        "Closing ScienceDirect tabs, clearing Chrome cache and cookies, "
        "and restarting Chrome.\n"
        f"The current article will be retried ({retry_number}/"
        f"{SCIENCEDIRECT_REQUEST_ERROR_MAX_RETRIES}).\n"
        "============================================================",
        flush=True,
    )
    log_event(
        {
            "event": "sciencedirect_request_error_recovery",
            "pmid": pmid,
            "url": url,
            "retry_number": retry_number,
            "started_at": now_timestamp(),
        }
    )

    close_sciencedirect_tabs()
    cleared = clear_browser_cache(clear_cookies=True, restart_chrome=True)
    if not cleared:
        print(
            "  [RECOVERY FAILED] Chrome cleanup/restart did not fully complete.",
            flush=True,
        )
        return False

    if DOWNLOAD_BREAK_SECONDS > 0:
        print(
            f"  [RECOVERY WAIT] Waiting {DOWNLOAD_BREAK_SECONDS}s before "
            "retrying the same article.",
            flush=True,
        )
        time.sleep(DOWNLOAD_BREAK_SECONDS)
    print("  [RECOVERY RETRY] Reopening the same ScienceDirect article.", flush=True)
    return True


def publisher_pdf_click_rule(url: str) -> PublisherClickRule | None:
    host = (urlparse(url).hostname or "").casefold()
    if host.endswith("sciencedirect.com") or host == SCIENCEDIRECT_PROXY_HOST:
        return PublisherClickRule(
            publisher="ScienceDirect",
            pdf_selector=(
                'a[aria-label^="View PDF"][href*="/pdfft"], a[href*="/pdfft"]'
            ),
            dismiss_selector=(
                '#pendo-base button._pendo-close-guide[aria-label="Close"], '
                'button[id^="pendo-close-guide-"][aria-label="Close"]'
            ),
            request_error_selector=".error-card .card-content h1.u-h2",
            request_error_text=(
                "There was a problem providing the content you requested"
            ),
        )
    if host in ("pubs.acs.org", ACS_PROXY_HOST):
        return PublisherClickRule(
            publisher="ACS",
            pdf_selector=(
                'a[data-id="article_header_OpenPDF"][href*="/doi/pdf/"], '
                'a.article__btn__secondary--pdf[href*="/doi/pdf/"]'
            ),
        )
    if is_wiley_host(host):
        return PublisherClickRule(
            publisher="Wiley",
            pdf_selector=(
                'a.pdf-download[href*="/doi/epdf/"], '
                'a[title="ePDF"][href*="/doi/epdf/"]'
            ),
            download_selector=(
                'a.navbar-download[href*="/doi/pdfdirect/"]'
                '[data-single-download="true"], '
                'a.navbar-download[href*="/doi/pdfdirect/"], '
                'div.navbar-download a.download[data-download-files-key="pdf"]'
                '[href*="/doi/pdfdirect/"], '
                'a.download[data-download-files-key="pdf"]'
                '[href*="/doi/pdfdirect/"]'
            ),
        )
    if is_sage_host(host):
        return PublisherClickRule(
            publisher="SAGE",
            pdf_selector=(
                'a[data-id="article-toolbar-pdf-epub"][href*="/doi/reader/"], '
                'a[href*="/doi/reader/"]'
            ),
            download_selector=(
                'a#favourite-download[href*="/doi/pdf/"], '
                'a[aria-label="Download PDF"][href*="/doi/pdf/"]'
            ),
        )
    if host == "pubs.rsc.org" or host.endswith(
        "-rsc-org.proxy.lib.ohio-state.edu"
    ):
        return PublisherClickRule(
            publisher="RSC",
            pdf_selector=(
                'a.article-pdfLink[href*="/article-pdf/"], '
                'a[data-doctype="contentPdf"][href*="/article-pdf/"]'
            ),
        )
    if is_tandfonline_host(host):
        return PublisherClickRule(
            publisher="Taylor & Francis",
            pdf_selector=(
                'a.show-pdf[href*="/doi/epdf/"], '
                'a[role="button"][href*="/doi/epdf/"]'
            ),
            download_selector=(
                'a.download[data-download-files-key="pdf"][href*="/doi/pdf/"], '
                'a[data-single-download="true"][href*="/doi/pdf/"]'
            ),
            reveal_download_selector=(
                'button#new-download-btn[aria-label="Download"], '
                'button[aria-label="Download"][aria-haspopup="true"]'
            ),
        )
    if host == "academic.oup.com" or host.endswith(
        "-oup-com.proxy.lib.ohio-state.edu"
    ):
        return PublisherClickRule(
            publisher="Oxford Academic",
            pdf_selector=(
                'li.item-pdf a.article-pdfLink[href*="/article-pdf/"], '
                'a.article-pdfLink[href*="/article-pdf/"]'
            ),
        )
    if host == "iopscience.iop.org" or host.endswith(
        "-iop-org.proxy.lib.ohio-state.edu"
    ):
        return PublisherClickRule(
            publisher="IOPscience",
            pdf_selector=(
                'a.content-download[href*="/article/"][href$="/pdf"], '
                'a[itemprop="sameAs"][href$="/pdf"]'
            ),
        )
    if is_public_or_osu_proxy_host(host, "iiarjournals.org"):
        return PublisherClickRule(
            publisher="IIAR Journals",
            pdf_selector=(
                'a[data-trigger="tab-pdf"][href$=".full.pdf"], '
                'a[href*="/content/anticanres/"][href$=".full.pdf"]'
            ),
        )
    if is_public_or_osu_proxy_host(host, "aacrjournals.org"):
        return PublisherClickRule(
            publisher="AACR Journals",
            pdf_selector=(
                'a.article-pdfLink[data-doctype="contentPdf"]'
                '[href*="/article-pdf/"], '
                'a.article-pdfLink[href*="/article-pdf/"]'
            ),
        )
    if is_public_or_osu_proxy_host(host, "jamanetwork.com"):
        return PublisherClickRule(
            publisher="JAMA Network",
            pdf_selector=(
                'a#pdf-link.js-pdfaccess[data-article-url$=".pdf"], '
                'a.js-pdfaccess[aria-label="Download PDF"]'
                '[data-article-url$=".pdf"]'
            ),
        )
    return None


def download_strategy(url: str) -> DownloadStrategy:
    """Classify a prepared URL into the automatic or manual-review tier."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()

    manual_pattern = matching_manual_pattern(url)
    if manual_pattern:
        return DownloadStrategy(
            priority=MANUAL_REVIEW_PRIORITY,
            automatic=False,
            label=f"configured manual pattern: {manual_pattern}",
        )

    # These viewers use opaque, session-dependent controls. Keep them visible
    # for a human instead of attempting an unsafe speculative click.
    if host.endswith("ebsco-com.proxy.lib.ohio-state.edu"):
        return DownloadStrategy(
            priority=MANUAL_REVIEW_PRIORITY,
            automatic=False,
            label="EBSCO institutional viewer",
        )

    click_rule = publisher_pdf_click_rule(url)
    if click_rule is not None:
        return DownloadStrategy(
            priority=AUTO_PUBLISHER_PRIORITY.get(
                click_rule.publisher,
                DIRECT_PDF_PRIORITY - 1,
            ),
            automatic=True,
            label=click_rule.publisher,
        )

    direct_pdf_patterns = (
        "/article-pdf/",
        "/content/pdf/",
        "/doi/pdf/",
        "/pdf/",
        "/pdfft/",
    )
    if path.endswith(".pdf") or any(
        pattern in path for pattern in direct_pdf_patterns
    ):
        return DownloadStrategy(
            priority=DIRECT_PDF_PRIORITY,
            automatic=True,
            label="direct PDF URL",
        )

    return DownloadStrategy(
        priority=MANUAL_REVIEW_PRIORITY,
        automatic=False,
        label=host or "unresolved publisher",
    )


def open_publisher_and_click_pdf(
    url: str,
    publisher: str,
    selector: str,
    download_selector: str | None,
    reveal_download_selector: str | None,
    dismiss_selector: str | None = None,
    request_error_selector: str | None = None,
    request_error_text: str | None = None,
) -> PublisherOpenStatus:
    """Open a publisher article and click through to its final PDF download."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return PublisherOpenStatus.AUTOMATION_FAILED

    attempts = max(1, int(PUBLISHER_CLICK_TIMEOUT_SECONDS / 0.5))
    javascript = r"""
(() => {
  const downloadSelector = __DOWNLOAD_SELECTOR__;
  const revealDownloadSelector = __REVEAL_DOWNLOAD_SELECTOR__;
  const dismissSelector = __DISMISS_SELECTOR__;
  const requestErrorSelector = __REQUEST_ERROR_SELECTOR__;
  const requestErrorText = __REQUEST_ERROR_TEXT__;
  if (requestErrorSelector && requestErrorText) {
    const requestError = document.querySelector(requestErrorSelector);
    const normalizedErrorText = requestError?.textContent
      ?.replace(/\s+/g, ' ')
      .trim();
    if (normalizedErrorText?.includes(requestErrorText)) {
      return 'request_blocked';
    }
  }

  if (dismissSelector) {
    const dismissButton = document.querySelector(dismissSelector);
    if (
      dismissButton &&
      dismissButton.dataset.automatedDismissClick !== 'true'
    ) {
      dismissButton.dataset.automatedDismissClick = 'true';
      dismissButton.click();
      return 'popup_dismissed';
    }
  }

  if (downloadSelector) {
    const downloadLink = document.querySelector(downloadSelector);
    if (downloadLink) {
      downloadLink.target = '_self';
      downloadLink.click();
      return 'download_clicked';
    }
  }

  if (revealDownloadSelector) {
    const revealButton = document.querySelector(revealDownloadSelector);
    if (revealButton && revealButton.dataset.automatedRevealClick !== 'true') {
      revealButton.dataset.automatedRevealClick = 'true';
      revealButton.click();
      return 'download_menu_opened';
    }
  }

  const link = document.querySelector(__PDF_SELECTOR__);
  if (!link) return 'waiting';
  if (link.dataset.automatedPdfClick === 'true') return 'waiting';
  link.dataset.automatedPdfClick = 'true';
  link.target = '_self';
  link.click();
  if (requestErrorSelector && requestErrorText) {
    return 'download_clicked_monitoring';
  }
  return downloadSelector ? 'pdf_page_opened' : 'download_clicked';
})()
""".replace(
        "__DOWNLOAD_SELECTOR__", json.dumps(download_selector)
    ).replace(
        "__REVEAL_DOWNLOAD_SELECTOR__", json.dumps(reveal_download_selector)
    ).replace(
        "__DISMISS_SELECTOR__", json.dumps(dismiss_selector)
    ).replace(
        "__REQUEST_ERROR_SELECTOR__", json.dumps(request_error_selector)
    ).replace(
        "__REQUEST_ERROR_TEXT__", json.dumps(request_error_text)
    ).replace(
        "__PDF_SELECTOR__", json.dumps(selector)
    ).strip()
    apple_script = f"""
on run argv
    set targetUrl to item 1 of argv
    set clickScript to item 2 of argv

    tell application "Google Chrome"
        if (count of windows) is 0 then make new window
        set articleTab to make new tab at end of tabs of front window with properties {{URL:targetUrl}}
        set active tab index of front window to (count of tabs of front window)
        set pdfPageOpened to false
        set popupDismissed to false
        set monitoredDownloadChecksRemaining to 0
        set lastJavaScriptError to ""

        repeat {attempts} times
            delay 0.5
            try
                set clickResult to execute articleTab javascript clickScript
                if clickResult is "request_blocked" then return "request_blocked"
                if clickResult is "download_clicked" then
                    if popupDismissed then return "download_clicked_after_popup"
                    return "download_clicked"
                end if
                if clickResult is "download_clicked_monitoring" then
                    set monitoredDownloadChecksRemaining to 20
                else if monitoredDownloadChecksRemaining > 0 then
                    set monitoredDownloadChecksRemaining to monitoredDownloadChecksRemaining - 1
                    if monitoredDownloadChecksRemaining is 0 then
                        if popupDismissed then return "download_clicked_after_popup"
                        return "download_clicked"
                    end if
                end if
                if clickResult is "pdf_page_opened" then set pdfPageOpened to true
                if clickResult is "download_menu_opened" then set pdfPageOpened to true
                if clickResult is "popup_dismissed" then set popupDismissed to true
            on error errorMessage
                set lastJavaScriptError to errorMessage
            end try
        end repeat

        if pdfPageOpened then return "download_link_not_found"
        if lastJavaScriptError is not "" then return "javascript_error: " & lastJavaScriptError
        return "view_pdf_not_found"
    end tell
end run
"""

    try:
        result = subprocess.run(
            ["osascript", "-e", apple_script, url, javascript],
            check=False,
            capture_output=True,
            text=True,
            timeout=PUBLISHER_CLICK_TIMEOUT_SECONDS + 10,
        )
    except subprocess.TimeoutExpired:
        print(f"  [AUTO-PDF] {publisher} automation timed out")
        return PublisherOpenStatus.AUTOMATION_FAILED
    except OSError as exc:
        print(f"  [AUTO-PDF] Chrome automation unavailable: {exc}")
        return PublisherOpenStatus.AUTOMATION_FAILED

    status = result.stdout.strip()
    if status == "request_blocked":
        print(
            f"  [AUTO-PDF] detected {publisher} request-limit error page",
            flush=True,
        )
        return PublisherOpenStatus.REQUEST_BLOCKED

    if status in {"download_clicked", "download_clicked_after_popup"}:
        if status == "download_clicked_after_popup":
            print(f"  [AUTO-PDF] dismissed {publisher} popup")
        print(f"  [AUTO-PDF] clicked {publisher} PDF download")
        return PublisherOpenStatus.OPENED

    if status.startswith("javascript_error:"):
        print("  [AUTO-PDF] article opened, but Chrome blocked the automatic click")
        print("  [AUTO-PDF] enable View > Developer > Allow JavaScript from Apple Events")
        return PublisherOpenStatus.OPENED

    if status == "view_pdf_not_found":
        print(f"  [AUTO-PDF] {publisher} PDF link was not found; article left open")
        return PublisherOpenStatus.OPENED

    if status == "download_link_not_found":
        print(f"  [AUTO-PDF] {publisher} PDF opened, but its download link was not found")
        return PublisherOpenStatus.OPENED

    error = result.stderr.strip() or status or f"osascript exited {result.returncode}"
    print(f"  [AUTO-PDF] Chrome automation failed: {error}")
    return PublisherOpenStatus.AUTOMATION_FAILED


def open_url(url: str) -> PublisherOpenStatus:
    if OPEN_COMMAND:
        subprocess.run([*shlex.split(OPEN_COMMAND), url], check=False)
        return PublisherOpenStatus.OPENED

    if AUTOMATE_PUBLISHER_PDF_CLICK and download_strategy(url).automatic:
        click_rule = publisher_pdf_click_rule(url)
        if click_rule:
            open_status = open_publisher_and_click_pdf(
                url,
                click_rule.publisher,
                click_rule.pdf_selector,
                click_rule.download_selector,
                click_rule.reveal_download_selector,
                click_rule.dismiss_selector,
                click_rule.request_error_selector,
                click_rule.request_error_text,
            )
            if open_status is not PublisherOpenStatus.AUTOMATION_FAILED:
                return open_status

    webbrowser.open_new_tab(url)
    return PublisherOpenStatus.OPENED


def existing_pdf_path(pmid: str) -> Path | None:
    candidate = OUTPUT_DIR / f"{pmid}.pdf"
    return candidate if candidate.is_file() and is_pdf_file(candidate) else None


def now_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_result(
    row: dict[str, str],
    *,
    status: str,
    source: str = "browser",
    error: str = "",
    started_at: str = "",
    finished_at: str = "",
    filename: str = "",
    url: str = "",
    output_path: str = "",
) -> None:
    """Write a complete result state, clearing fields from any prior attempt."""
    row.update(
        {
            FETCH_STATUS_COLUMN: status,
            FETCH_SOURCE_COLUMN: source,
            FETCH_ERROR_COLUMN: error,
            DOWNLOAD_STARTED_AT_COLUMN: started_at,
            DOWNLOAD_FINISHED_AT_COLUMN: finished_at,
            DOWNLOAD_FILENAME_COLUMN: filename,
            DOWNLOAD_URL_COLUMN: url,
            OUTPUT_PATH_COLUMN: output_path,
        }
    )


def build_queue(
    rows: list[dict[str, str]], *, require_target_flag: bool
) -> list[dict[str, str]]:
    eligible: list[tuple[dict[str, str], str, str]] = []
    seen_pmids: set[str] = set()

    for row in rows:
        if require_target_flag and not is_yes(row.get(IS_FETCH_TARGET_COLUMN)):
            continue

        raw_pmid = normalize_cell(row.get(PMID_COLUMN))
        if not raw_pmid:
            continue
        pmid = normalize_pmid(raw_pmid)
        if not pmid:
            write_result(
                row,
                status="failed",
                source="validation",
                error="invalid_pmid",
            )
            continue
        if pmid in seen_pmids:
            write_result(
                row,
                status="skipped",
                source="validation",
                error="duplicate_pmid",
            )
            continue
        seen_pmids.add(pmid)

        existing = existing_pdf_path(pmid)
        if existing is not None:
            write_result(
                row,
                status="success",
                source="existing",
                filename=existing.name,
                url=normalize_cell(row.get(DOWNLOAD_URL_COLUMN)),
                output_path=str(existing),
            )
            continue

        doi = normalize_doi(row.get(DOI_COLUMN))
        if not doi:
            continue
        if not is_http_url(doi) and not doi.casefold().startswith("10."):
            write_result(
                row,
                status="failed",
                source="validation",
                error="invalid_doi",
            )
            continue

        eligible.append((row, pmid, doi))

    selected = eligible[BATCH_START:BATCH_START + BATCH_LIMIT]
    queue: list[dict[str, str]] = []
    for row, pmid, doi in selected:
        existing_url = normalize_cell(row.get(DOWNLOAD_URL_COLUMN))
        override_url = institutional_url_override(doi)
        if override_url:
            url = override_url
            if url != existing_url:
                print(f"  [OVERRIDE] doi={doi} -> {url}")
        elif is_http_url(existing_url):
            url = rewrite_resolved_url(existing_url)
            if url != existing_url:
                print(f"  [REWRITE] {existing_url} -> {url}")
        else:
            url = resolve_best_pdf_url(doi)

        blocked_pattern = matching_blocked_pattern(url)
        if blocked_pattern:
            print(f"  [SKIP URL] pmid={pmid} pattern={blocked_pattern} url={url}")
            write_result(
                row,
                status="skipped",
                source="skip_rule",
                error=f"blocked_url_pattern:{blocked_pattern}",
                url=url,
            )
            continue

        write_result(row, status="pending", url=url)
        queue.append(row)

    queue.sort(
        key=lambda queued_row: download_strategy(
            normalize_cell(queued_row.get(DOWNLOAD_URL_COLUMN))
        ).priority
    )
    return queue


def main() -> int:
    if BATCH_START < 0 or BATCH_LIMIT <= 0:
        print("BATCH_START must be >= 0 and BATCH_LIMIT must be > 0")
        return 1
    if WAIT_TIMEOUT_SECONDS <= 0 or POLL_INTERVAL_SECONDS <= 0:
        print("Download timeout and polling interval must be positive")
        return 1
    if AUTO_CLEAR_BROWSER_CACHE and CACHE_CLEAR_EVERY_FILES <= 0:
        print("CACHE_CLEAR_EVERY_FILES must be > 0 when cache cleanup is enabled")
        return 1
    if DOWNLOAD_BREAK_EVERY_FILES <= 0 or DOWNLOAD_BREAK_SECONDS < 0:
        print("Download break interval must be > 0 and duration must be >= 0")
        return 1
    if SCIENCEDIRECT_REQUEST_ERROR_MAX_RETRIES < 0:
        print("SCIENCEDIRECT_REQUEST_ERROR_MAX_RETRIES must be >= 0")
        return 1
    if not INPUT_CSV.exists():
        print(f"Input CSV not found: {INPUT_CSV}")
        return 1

    ensure_directory(WATCH_DIR)
    ensure_directory(OUTPUT_DIR)

    try:
        fieldnames, rows = load_csv_rows(INPUT_CSV)
    except (OSError, ValueError) as exc:
        print(f"Unable to read input CSV: {exc}")
        return 1
    if not rows:
        print("Input CSV is empty.")
        return 0
    if PMID_COLUMN not in fieldnames:
        print(f"Missing required column: {PMID_COLUMN}")
        return 1

    require_target_flag = any(normalize_cell(row.get(IS_FETCH_TARGET_COLUMN)) for row in rows)
    fieldnames = ensure_columns(
        fieldnames,
        [*DOWNLOAD_RESULT_FIELDS, IS_FETCH_TARGET_COLUMN, DOI_COLUMN],
    )
    queue = build_queue(rows, require_target_flag=require_target_flag)

    # Persist validation, existing-file, skip-rule, and pending states even when
    # there are no automatic browser targets.
    if not DRY_RUN:
        write_csv_rows(OUTPUT_CSV, fieldnames, rows)

    if not queue:
        print("No valid browser targets in this batch.")
        return 0

    print(f"[BATCH] count={len(queue)} start={BATCH_START} limit={BATCH_LIMIT}")
    automatic_count = sum(
        download_strategy(normalize_cell(row.get(DOWNLOAD_URL_COLUMN))).automatic
        for row in queue
    )
    print(
        f"[ORDER] automatic={automatic_count} "
        f"manual_review={len(queue) - automatic_count}"
    )
    print(f"[WATCH] {WATCH_DIR}")
    print(f"[OUT]   {OUTPUT_DIR}")
    if AUTO_CLEAR_BROWSER_CACHE:
        cleanup_contents = (
            "cache and cookies" if CLEAR_COOKIES_WITH_CACHE else "cache only"
        )
        cleanup_publishers = ", ".join(CACHE_CLEAR_PUBLISHERS) or "none"
        print(
            f"[CLEANUP SCHEDULE] Chrome {cleanup_contents} clears after every "
            f"{CACHE_CLEAR_EVERY_FILES} successful new PDFs from: "
            f"{cleanup_publishers} "
            f"(Chrome profile: {CHROME_PROFILE_DIRECTORY})."
        )
    print(
        f"[DOWNLOAD BREAK] Close ScienceDirect tabs and wait "
        f"{DOWNLOAD_BREAK_SECONDS}s after every {DOWNLOAD_BREAK_EVERY_FILES} "
        "successful new PDFs."
    )

    successful_downloads = 0
    successful_cleanup_downloads_by_publisher: dict[str, int] = {}

    for position, row in enumerate(queue, start=1):
        pmid = normalize_pmid(row.get(PMID_COLUMN))
        url = normalize_cell(row.get(DOWNLOAD_URL_COLUMN))
        strategy = download_strategy(url)
        destination = OUTPUT_DIR / f"{pmid}.pdf"

        print(f"\n[{position}/{len(queue)}] pmid={pmid}")
        mode = "automatic" if strategy.automatic else "manual review"
        print(f"  [MODE] {mode}: {strategy.label}")
        print(f"  URL: {url}")
        if not strategy.automatic:
            print("  [ACTION] complete any login, access, or PDF clicks in the browser")

        if destination.exists() and is_pdf_file(destination):
            print(f"  [SKIP] already exists: {destination.name}")
            write_result(
                row,
                status="success",
                source="existing",
                filename=destination.name,
                url=url,
                output_path=str(destination),
            )
            if not DRY_RUN:
                write_csv_rows(OUTPUT_CSV, fieldnames, rows)
            continue

        known = snapshot_completed_files(WATCH_DIR)
        started_at = now_timestamp()
        log_event({"event": "open", "pmid": pmid, "url": url, "started_at": started_at})

        request_error_failure = ""
        if not DRY_RUN:
            request_error_retries = 0
            while True:
                open_status = open_url(url)
                time.sleep(OPEN_DELAY_SECONDS)
                if open_status is not PublisherOpenStatus.REQUEST_BLOCKED:
                    break

                if (
                    request_error_retries
                    >= SCIENCEDIRECT_REQUEST_ERROR_MAX_RETRIES
                ):
                    request_error_failure = (
                        "sciencedirect_request_error_retries_exhausted"
                    )
                    break

                request_error_retries += 1
                if not recover_from_sciencedirect_request_error(
                    pmid=pmid,
                    url=url,
                    retry_number=request_error_retries,
                ):
                    request_error_failure = (
                        "sciencedirect_request_error_cleanup_failed"
                    )
                    break

        if DRY_RUN:
            continue

        if request_error_failure:
            finished_at = now_timestamp()
            print(
                "  [FAILED] ScienceDirect request-error recovery stopped: "
                f"{request_error_failure}",
                flush=True,
            )
            log_event(
                {
                    "event": "failed",
                    "pmid": pmid,
                    "url": url,
                    "reason": request_error_failure,
                    "started_at": started_at,
                    "finished_at": finished_at,
                }
            )
            write_result(
                row,
                status="failed",
                error=request_error_failure,
                started_at=started_at,
                finished_at=finished_at,
                url=url,
            )
            write_csv_rows(OUTPUT_CSV, fieldnames, rows)
            continue

        while True:
            skip_keys = "'s'" if os.name == "nt" else "'s' + Enter"
            hint = f"| press {skip_keys} to skip" if INTERACTIVE_SKIP else ""
            print(f"  [WAIT] {WAIT_TIMEOUT_SECONDS}s {hint}".rstrip())
            new_file, skipped = wait_for_download(known)

            if skipped:
                finished_at = now_timestamp()
                print("  [SKIP] manually skipped")
                log_event(
                    {
                        "event": "skipped",
                        "pmid": pmid,
                        "url": url,
                        "reason": "manually_skipped",
                        "started_at": started_at,
                        "finished_at": finished_at,
                    }
                )
                write_result(
                    row,
                    status="skipped",
                    error="manually_skipped",
                    started_at=started_at,
                    finished_at=finished_at,
                    url=url,
                )
                write_csv_rows(OUTPUT_CSV, fieldnames, rows)
                break

            if new_file:
                finished_at = now_timestamp()
                print(f"  [MOVE] {new_file.name} -> {destination.name}")
                if new_file.resolve() != destination.resolve():
                    if destination.exists():
                        destination.unlink()
                    shutil.move(str(new_file), str(destination))

                log_event(
                    {
                        "event": "success",
                        "pmid": pmid,
                        "url": url,
                        "source_file": new_file.name,
                        "output_path": str(destination),
                        "started_at": started_at,
                        "finished_at": finished_at,
                    }
                )
                write_result(
                    row,
                    status="success",
                    started_at=started_at,
                    finished_at=finished_at,
                    filename=destination.name,
                    url=url,
                    output_path=str(destination),
                )
                write_csv_rows(OUTPUT_CSV, fieldnames, rows)
                if CLOSE_COMPLETED_AUTOMATIC_TABS and strategy.automatic:
                    close_completed_automatic_tab(url)
                successful_downloads += 1
                if (
                    CLOSE_SCIENCEDIRECT_TABS_AT_BREAK
                    and download_break_is_due(successful_downloads)
                ):
                    close_sciencedirect_tabs()
                if publisher_uses_scheduled_cleanup(strategy.label):
                    publisher_successes = (
                        successful_cleanup_downloads_by_publisher.get(
                            strategy.label,
                            0,
                        )
                        + 1
                    )
                    successful_cleanup_downloads_by_publisher[strategy.label] = (
                        publisher_successes
                    )
                    maybe_clear_browser_cache(
                        publisher_successes,
                        publisher=strategy.label,
                    )
                wait_for_scheduled_download_break(successful_downloads)
                break

            print("  [TIMEOUT] no download detected")
            if INTERACTIVE_SKIP:
                choice = input("  Retry (r) or skip (s)? ").strip().lower()
                if choice == "r":
                    continue

            finished_at = now_timestamp()
            log_event(
                {
                    "event": "failed",
                    "pmid": pmid,
                    "url": url,
                    "reason": "timeout",
                    "started_at": started_at,
                    "finished_at": finished_at,
                }
            )
            write_result(
                row,
                status="failed",
                error="timeout",
                started_at=started_at,
                finished_at=finished_at,
                url=url,
            )
            write_csv_rows(OUTPUT_CSV, fieldnames, rows)
            break

    if DRY_RUN:
        print("Dry run complete; CSV and downloaded files were not changed.")
    else:
        print(f"Output written to: {OUTPUT_CSV}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
