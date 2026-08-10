from __future__ import annotations

import csv
import json
import os
import random
import select
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent
RUN_ID = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")

PMID_COLUMN = "pmid"
DOI_COLUMN = "doi"
IS_FETCH_TARGET_COLUMN = "is_fetch_target"
FETCH_STATUS_COLUMN = "fetch_status"
FETCH_SOURCE_COLUMN = "fetch_source"
FETCH_ERROR_COLUMN = "fetch_error"
FETCH_ERROR_CATEGORY_COLUMN = "fetch_error_category"
FETCH_ERROR_CODE_COLUMN = "fetch_error_code"
FETCH_ERROR_DETAIL_COLUMN = "fetch_error_detail"
FETCH_ERROR_RETRYABLE_COLUMN = "fetch_error_retryable"
FETCH_ERROR_ACTION_COLUMN = "fetch_error_action"
FETCH_PUBLISHER_COLUMN = "fetch_publisher"
FETCH_PUBLISHER_FAILURE_COUNT_COLUMN = "fetch_publisher_failure_count"
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
    FETCH_ERROR_CATEGORY_COLUMN,
    FETCH_ERROR_CODE_COLUMN,
    FETCH_ERROR_DETAIL_COLUMN,
    FETCH_ERROR_RETRYABLE_COLUMN,
    FETCH_ERROR_ACTION_COLUMN,
    FETCH_PUBLISHER_COLUMN,
    FETCH_PUBLISHER_FAILURE_COUNT_COLUMN,
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
    download_unavailable_selector: str | None = None
    download_unavailable_texts: tuple[str, ...] = ()
    direct_navigation: bool = False
    download_unavailable_statuses: tuple[int, ...] = ()
    download_ready_selector: str | None = None


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
    DOWNLOAD_UNAVAILABLE = "download_unavailable"
    AUTOMATION_FAILED = "automation_failed"
    HTTP_ERROR = "http_error"


@dataclass(frozen=True)
class PublisherOpenResult:
    status: PublisherOpenStatus
    http_status: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class FailureDetails:
    category: str
    code: str
    detail: str
    retryable: bool
    recommended_action: str


@dataclass(frozen=True)
class PublisherCircuit:
    publisher: str
    signal: str
    failure_count: int
    threshold: int
    opened_at: str


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

    # urlparse() treats the semicolon suffix in legacy Wiley DOIs such as
    # ``...3.0.CO;2-4`` as URL parameters and removes it from ``path``.
    # urlsplit() keeps the complete DOI path intact.
    parsed = urlsplit(cleaned)
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


def is_doi_resolver_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and (parsed.hostname or "").casefold() in {"doi.org", "dx.doi.org"}
    )


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
# Rebuild each queued row's download_url from its DOI instead of reusing the
# value already stored in the CSV.  This can repair stale or incomplete URLs
# and retry DOI resolution after a transient failure.  Set to False when the
# saved download_url values are known to be good.
OVERRIDE_EXISTING_DOWNLOAD_URLS = False
OPEN_DELAY_SECONDS = 1.5
WAIT_TIMEOUT_SECONDS = 10
POLL_INTERVAL_SECONDS = 1
MIN_FILE_SIZE_BYTES = 1024
INTERACTIVE_SKIP = True
# Opt-in unattended mode. After the normal wait expires, a record is marked
# skipped and the batch continues without asking for retry/skip input.
AUTO_SKIP_MODE = False
# Publishers listed here retain the interactive retry/skip prompt even when
# AUTO_SKIP_MODE is enabled. Names must match PublisherClickRule.publisher.
AUTO_SKIP_PUBLISHER_EXCEPTIONS: set[str] = set()
DRY_RUN = False
OPEN_COMMAND: str | None = None
# Append-only, machine-readable history for later failure-pattern analysis.
BATCH_LOG_PATH: Path | None = PROJECT_ROOT / "download_events.jsonl"
# Matching URLs are recorded as skipped instead of silently disappearing.
BLOCKED_URL_PATTERNS = ["karger.com", "10.1159", "ashpublications.org", "ascopubs.org", "neurology.org", "auajournals.org", "10.1158", "ovid.com", "jamaoto", "jamaoncol", "eurekaselect.com", "ersnet.org", "10.23736", "degruyterbrill.com", "dustri.com"]
# Matching URLs remain in the queue but run in the manual-review tier.
MANUAL_URL_PATTERNS = [] #"link.springer.com"
AUTOMATE_PUBLISHER_PDF_CLICK = True
PUBLISHER_CLICK_TIMEOUT_SECONDS = 20
DOI_RESOLUTION_MIN_INTERVAL_SECONDS = 1.5
DOI_RESOLUTION_JITTER_SECONDS = 0.75
DOI_RESOLUTION_DEFAULT_RATE_LIMIT_PAUSE_SECONDS = 60
DOI_RESOLUTION_MAX_RETRY_AFTER_SECONDS = 300
AUTO_CLEAR_BROWSER_CACHE = True
CACHE_CLEAR_EVERY_FILES = 12
CACHE_CLEAR_PUBLISHERS = ["ScienceDirect"]
CLEAR_COOKIES_WITH_CACHE = True
DOWNLOAD_BREAK_EVERY_FILES = 2000
DOWNLOAD_BREAK_SECONDS = 60
SCIENCEDIRECT_REQUEST_ERROR_MAX_RETRIES = 3
# Publisher circuits only use explicit response/block signals. Ordinary
# timeouts, 401s, and 404s never disable an entire publisher.
PUBLISHER_CIRCUIT_THRESHOLDS = {
    "http_429": 3,
    "request_blocked": 3,
    "http_403": 4,
    "http_502": 4,
    "http_503": 4,
    "http_504": 4,
}
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
    "Springer Nature": 0,
    "Wiley": 5,
    "ACS": 10,
    "ASCO Publications": 15,
    "Oxford Academic": 20,
    "Taylor & Francis": 25,
    "SAGE": 30,
    "RSC": 40,
    "IOPscience": 70,
    "IIAR Journals": 80,
    "AACR Journals": 90,
    "JAMA Network": 100,
    "NEJM": 105,
    "Ovid": 110,
    "AUA Journals": 115,
    "Nature": 120,
    "Haematologica": 125,
    "Cancer Research and Treatment": 130,
    "BMJ": 135,
    "JCI Insight": 140,
    "PLOS": 145,
    "Oncotarget": 147,
    "ACP Journals": 149,
    "ScienceDirect": 150,
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
    "10.1212/WNL.0000000000003400": (
        "https://oce-ovid-com.proxy.lib.ohio-state.edu/"
        "article/00006114-201612060-00006/HTML"
    ),
    "10.1016/j.juro.2011.03.129": (
        "https://www-sciencedirect-com.proxy.lib.ohio-state.edu/"
        "science/article/pii/S0022534711035427"
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
    ("10.1089/", f"{LIEBERT_ARTICLE_BASE_URL}/{{doi}}"),
    ("10.1080/", f"{TANDF_ARTICLE_BASE_URL}/{{doi}}"),
    ("10.1088/", "https://iopscience.iop.org/article/{doi}"),
]

# When a network DOI lookup falls back to doi.org, these stable ownership
# hints let browser automation select the correct publisher rule after Chrome
# follows the redirect. They also let blocked publisher domains match before
# the browser opens the DOI URL.
DOI_REDIRECT_HOST_HINTS: list[tuple[str, str]] = [
    ("10.1016/", "www.sciencedirect.com"),
    ("10.1039/", "pubs.rsc.org"),
    ("10.1001/", "jamanetwork.com"),
    ("10.1093/", "academic.oup.com"),
    ("10.1097/", "www.ovid.com"),
    ("10.1136/", "bmj.com"),
    ("10.1158/", "aacrjournals.org"),
    ("10.1186/s40425-", "bmj.com"),
    ("10.1200/", "ascopubs.org"),
    ("10.1210/", "academic.oup.com"),
    ("10.1182/", "ashpublications.org"),
    ("10.1212/", "www.neurology.org"),
    ("10.1371/", "journals.plos.org"),
    ("10.1177/", "journals.sagepub.com"),
    ("10.1086/340133", "academic.oup.com"),
    ("10.1634/", "academic.oup.com"),
    ("10.18632/", "www.oncotarget.com"),
    ("10.21873/", "iiarjournals.org"),
    ("10.2217/", "www.tandfonline.com"),
    ("10.3109/", "www.tandfonline.com"),
    ("10.3324/", "haematologica.org"),
    ("10.4143/", "www.e-crt.org"),
    ("10.7205/", "academic.oup.com"),
    ("10.7326/", "www.acpjournals.org"),
]


def publisher_host_hint_for_doi(value: str) -> str:
    doi = normalize_doi(value).casefold()
    for prefix, host in DOI_REDIRECT_HOST_HINTS:
        if doi.startswith(prefix.casefold()):
            return host
    return ""


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


def wiley_article_base_url(host: str) -> str:
    """Preserve Wiley journal subdomains when routing through OSU EZproxy."""
    public_suffix = ".onlinelibrary.wiley.com"
    if host.endswith(public_suffix):
        proxy_host = (
            f"{host.replace('.', '-')}.proxy.lib.ohio-state.edu"
        )
        return f"https://{proxy_host}/doi/full"
    if host.endswith("-onlinelibrary-wiley-com.proxy.lib.ohio-state.edu"):
        return f"https://{host}/doi/full"
    return WILEY_ARTICLE_BASE_URL


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


def rewrite_resolved_url(url: str, fallback_doi: str = "") -> str:
    # Keep legacy DOI semicolon suffixes in the path.  With urlparse(), a DOI
    # ending in ``.CO;2-X`` is exposed as path ``.CO`` plus params ``2-X``.
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path
    # Some publishers redirect automated DOI lookups to a cookie/error endpoint
    # on the correct host. In that case the original DOI remains authoritative.
    doi = doi_from_doi_path(path) or normalize_doi(fallback_doi)
    quoted_doi = encoded_doi(doi)

    if is_wiley_host(host) and doi:
        return f"{wiley_article_base_url(host)}/{quoted_doi}"

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

    # Literatum-based journal sites use this endpoint when an automated
    # redirect has no cookie context. Their canonical DOI route is uniform.
    if path.casefold().rstrip("/") == "/action/cookieabsent" and doi:
        return parsed._replace(
            path=f"/doi/full/{quoted_doi}",
            query="",
            fragment="",
        ).geturl()

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

    if path.casefold().rstrip("/") == "/crawlprevention/governor":
        content_path = parse_qs(parsed.query).get("content", [""])[0]
        if content_path.startswith("/") and not content_path.startswith("//"):
            return parsed._replace(
                path=content_path,
                query="",
                fragment="",
            ).geturl()

    if "error=cookies_not_supported" in parsed.query.casefold():
        return parsed._replace(query="", fragment="").geturl()

    return url


_last_doi_resolution_request_at = 0.0
_doi_resolution_cooldown_until = 0.0


def prepare_pdf_url_locally(doi: str) -> str:
    """Build the best available URL without making any network request."""
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

    return f"https://doi.org/{encoded_doi(doi)}"


def retry_after_seconds(exc: HTTPError) -> float | None:
    value = normalize_cell(exc.headers.get("Retry-After") if exc.headers else "")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def wait_for_doi_resolution_slot() -> bool:
    """Pace DOI lookups and wait out any server-requested cooldown."""
    global _last_doi_resolution_request_at
    now = time.monotonic()
    if now < _doi_resolution_cooldown_until:
        remaining = _doi_resolution_cooldown_until - now
        print(
            f"  [RESOLVE PAUSED] waiting {remaining:.1f}s before more DOI traffic",
            flush=True,
        )
        time.sleep(remaining)
        now = time.monotonic()

    minimum_gap = DOI_RESOLUTION_MIN_INTERVAL_SECONDS + random.uniform(
        0.0,
        DOI_RESOLUTION_JITTER_SECONDS,
    )
    elapsed = now - _last_doi_resolution_request_at
    if _last_doi_resolution_request_at and elapsed < minimum_gap:
        time.sleep(minimum_gap - elapsed)
    _last_doi_resolution_request_at = time.monotonic()
    return True


def resolve_best_pdf_url(doi: str, resolve_timeout: float = 8.0) -> str:
    """Resolve one DOI lazily; local rules never generate network traffic."""
    global _doi_resolution_cooldown_until
    doi = normalize_doi(doi)
    prepared_url = prepare_pdf_url_locally(doi)
    if not prepared_url or not is_doi_resolver_url(prepared_url):
        return prepared_url

    doi_url = prepared_url
    errors: list[str] = []
    resolved = ""
    resolved_method = ""
    for method in ("HEAD", "GET"):
        if not wait_for_doi_resolution_slot():
            errors.append("lookup paused after HTTP 429")
            break
        try:
            request = Request(
                doi_url,
                method=method,
                headers={
                    "User-Agent": "Mozilla/5.0 (browser-pdf-fetch/2.0)",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            with urlopen(request, timeout=resolve_timeout) as response:
                resolved = response.geturl()
            resolved_method = method
            break
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            errors.append(f"{method} {describe_resolution_error(exc)}")
            if isinstance(exc, HTTPError):
                if exc.code == 429:
                    requested_pause = retry_after_seconds(exc)
                    pause_seconds = (
                        DOI_RESOLUTION_DEFAULT_RATE_LIMIT_PAUSE_SECONDS
                        if requested_pause is None
                        else min(requested_pause, DOI_RESOLUTION_MAX_RETRY_AFTER_SECONDS)
                    )
                    _doi_resolution_cooldown_until = max(
                        _doi_resolution_cooldown_until,
                        time.monotonic() + pause_seconds,
                    )
                    print(
                        f"  [RESOLVE RATE LIMIT] pausing DOI lookups for "
                        f"{pause_seconds:.0f}s"
                    )
                    break
                # Do not turn a rejected HEAD into a second publisher request.
                # GET fallback is reserved for servers that reject HEAD itself.
                if exc.code not in {405, 501}:
                    break
            elif method == "HEAD":
                # Network errors and timeouts should not be doubled immediately.
                break

    if not resolved:
        if time.monotonic() < _doi_resolution_cooldown_until:
            remaining = _doi_resolution_cooldown_until - time.monotonic()
            print(
                f"  [RESOLVE PAUSED] waiting {remaining:.1f}s before browser fallback",
                flush=True,
            )
            time.sleep(remaining)
        print(
            f"  [RESOLVE FALLBACK] {'; '.join(errors)}; "
            f"using {doi_url}"
        )
        log_event(
            {
                "event": "doi_resolution_failed",
                "doi": doi,
                "url": doi_url,
                "errors": errors,
                "action": "open DOI URL in browser without another resolver request",
            }
        )
        return doi_url

    print(f"  [RESOLVE {resolved_method}] {doi_url} -> {resolved}")
    rewritten = rewrite_resolved_url(resolved, fallback_doi=doi)
    if rewritten != resolved:
        print(f"  [REWRITE] -> {rewritten}")
    log_event(
        {
            "event": "doi_resolved",
            "doi": doi,
            "method": resolved_method,
            "resolver_url": doi_url,
            "resolved_url": resolved,
            "rewritten_url": rewritten,
        }
    )
    return rewritten


def describe_resolution_error(exc: Exception) -> str:
    """Return a concise, single-line explanation for DOI lookup failures."""
    if isinstance(exc, HTTPError):
        if exc.code == 403:
            return "HTTP 403 (automated lookup refused)"
        if 300 <= exc.code < 400:
            return f"HTTP {exc.code} (redirect loop or rejected redirect)"
        return f"HTTP {exc.code} ({exc.reason})"
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, URLError):
        reason = " ".join(str(exc.reason).split())
        return f"network error ({reason})"
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__} ({message})"


def matching_blocked_pattern(url: str) -> str:
    candidate_values = [url.casefold()]
    if is_doi_resolver_url(url):
        hinted_host = publisher_host_hint_for_doi(url)
        if hinted_host:
            candidate_values.append(hinted_host.casefold())
    for pattern in BLOCKED_URL_PATTERNS:
        cleaned = pattern.strip()
        if cleaned and any(
            cleaned.casefold() in candidate for candidate in candidate_values
        ):
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


def expected_sciencedirect_pii(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    if not (host.endswith("sciencedirect.com") or host == SCIENCEDIRECT_PROXY_HOST):
        return ""
    if "/pii/" not in parsed.path:
        return ""
    return parsed.path.split("/pii/", 1)[1].strip("/").split("/", 1)[0]


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
    expected_url: str = "",
) -> tuple[Path | None, bool]:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    expected_pii = expected_sciencedirect_pii(expected_url).casefold()
    reported_mismatches: set[tuple[Path, int]] = set()
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
                if expected_pii and expected_pii not in path.name.casefold():
                    mismatch_key = (resolved_path, state.modified_ns)
                    if mismatch_key not in reported_mismatches:
                        print(
                            f"  [IGNORE PDF] {path.name} does not match "
                            f"ScienceDirect PII {expected_pii.upper()}"
                        )
                        reported_mismatches.add(mismatch_key)
                    continue
                candidates.append((path, state))

        if candidates:
            newest, _ = max(candidates, key=lambda item: item[1].modified_ns)
            return newest, False

        time.sleep(POLL_INTERVAL_SECONDS)

    return None, False


def log_event(payload: Mapping[str, object]) -> None:
    if BATCH_LOG_PATH is None:
        return
    event = dict(payload)
    event.setdefault("schema_version", 1)
    event.setdefault("run_id", RUN_ID)
    event.setdefault("recorded_at", now_timestamp())
    BATCH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BATCH_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


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
    if is_doi_resolver_url(url):
        expected_host = publisher_host_hint_for_doi(url) or expected_host
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
    if is_doi_resolver_url(url):
        hinted_host = publisher_host_hint_for_doi(url)
        if hinted_host:
            return publisher_pdf_click_rule(f"https://{hinted_host}/")
    if is_public_or_osu_proxy_host(host, "link.springer.com"):
        return PublisherClickRule(
            publisher="Springer Nature",
            pdf_selector='a[href$=".pdf"]',
            download_unavailable_selector=(
                'div.c-notes[data-test="c-notes"] p.c-notes__text, '
                'p.c-status-message--info a#test-login-banner-link, '
                'div[data-test="access-article"] '
                '[data-test="access-via-institution"], '
                'div[data-test="access-article"] '
                '[data-test-id="buy-article-darwin"]'
            ),
            download_unavailable_texts=(
                "This is a preview of subscription content",
                "log in via an institution",
                "Buy article PDF",
            ),
            direct_navigation=True,
            download_unavailable_statuses=(404,),
        )
    if host.endswith("sciencedirect.com") or host == SCIENCEDIRECT_PROXY_HOST:
        return PublisherClickRule(
            publisher="ScienceDirect",
            pdf_selector=(
                'a[aria-label^="View PDF"][href*="/pdfft"], '
                'a[href*="/pdfft"], '
                'li.ViewPDF a[href*="/science/article/pii/"][href*="/pdf"], '
                'a[aria-label^="View PDF"][href*="/pdf"], '
                'a[href*="/science/article/pii/"][href*="/pdf"]'
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
    if is_public_or_osu_proxy_host(host, "ascopubs.org"):
        return PublisherClickRule(
            publisher="ASCO Publications",
            pdf_selector=(
                'a.btn--pdf[href*="/doi/pdf/"], '
                'a[aria-label*="PDF"][href*="/doi/pdf/"], '
                'a[href*="/doi/pdf/"]'
            ),
            download_selector=(
                'a.embedded--download--btn[href*="/doi/pdfdirect/"], '
                'a[aria-label="Download PDF"][href*="/doi/pdfdirect/"], '
                '#main-content a[href*="/doi/pdfdirect/"], '
                'a[href*="/doi/pdfdirect/"]'
            ),
        )
    if is_public_or_osu_proxy_host(host, "www.nejm.org"):
        return PublisherClickRule(
            publisher="NEJM",
            pdf_selector=(
                'a.btn--pdf[href*="/doi/pdf/"], '
                'a[aria-label="View PDF"][href*="/doi/pdf/"], '
                'a[href*="/doi/pdf/"]'
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
            reveal_download_selector=(
                'button.dropdown-trigger[aria-label="Download document"]'
                '[aria-haspopup="true"], '
                '.navbar-download button.dropdown-trigger[aria-haspopup="true"]'
            ),
            download_unavailable_selector=(
                '.popup_container.backgroundNotice .text-container .text, '
                '.navbar-download .dropdown-content .dropdown-text'
            ),
            download_unavailable_texts=(
                "Downloading and printing are disabled",
                "You do not have permission to download",
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
                'a.navbar-download.format-download-btn'
                '[data-download-files-key="pdf"][href*="/doi/pdf/"], '
                'a.navbar-download[data-single-download="true"]'
                '[href*="/doi/pdf/"], '
                'a[data-download-files-key="pdf"][href*="/doi/pdf/"], '
                'a#favourite-download[href*="/doi/pdf/"], '
                'a[aria-label^="Download PDF"][href*="/doi/pdf/"]'
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
                'a[href*="/content/anticanres/"][href$=".full.pdf"], '
                'a.link-icon[href*="/content/invivo/"]'
                '[href$=".full-text.pdf"], '
                'a[href*="/content/"][href$=".full-text.pdf"]'
            ),
        )
    if is_public_or_osu_proxy_host(host, "insight.jci.org"):
        return PublisherClickRule(
            publisher="JCI Insight",
            pdf_selector=(
                'a[href*="/articles/view/"][href$="/pdf"]'
            ),
            download_selector=(
                'form#download_pdf_form[action$="/pdf/render.pdf"] button'
            ),
            download_ready_selector=(
                'form#download_pdf_form input[id^="recaptcha-token-"]'
            ),
        )
    if is_public_or_osu_proxy_host(host, "journals.plos.org"):
        return PublisherClickRule(
            publisher="PLOS",
            pdf_selector=(
                'div.dload-pdf a#downloadPdf'
                '[href*="/article/file?"][href*="type=printable"], '
                'a#downloadPdf[href*="/article/file?"]'
                '[href*="type=printable"]'
            ),
        )
    if is_public_or_osu_proxy_host(host, "oncotarget.com"):
        return PublisherClickRule(
            publisher="Oncotarget",
            pdf_selector=(
                'a.file[href*="/article/"][href*="/pdf/"], '
                'a[href*="/article/"][href*="/pdf/"]'
            ),
        )
    if is_public_or_osu_proxy_host(host, "acpjournals.org"):
        return PublisherClickRule(
            publisher="ACP Journals",
            pdf_selector=(
                'div.info-panel__formats a.btn--pdf'
                '[href*="/doi/reader/"], '
                'a.btn--pdf[aria-label="Open full-text in eReader"]'
                '[href*="/doi/reader/"]'
            ),
            download_selector=(
                'a.navbar-download[data-single-download="true"]'
                '[data-download-files-key="pdf"][href*="/doi/pdf/"], '
                'a[aria-label^="Download PDF"]'
                '[href*="/doi/pdf/"][href*="download=true"]'
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
    if (
        is_public_or_osu_proxy_host(host, "oce.ovid.com")
        or is_public_or_osu_proxy_host(host, "www.ovid.com")
    ):
        return PublisherClickRule(
            publisher="Ovid",
            pdf_selector=(
                "button.omni-article-tool__pdf, "
                'button#downloadpdf-button[aria-label="Download PDF"], '
                'button.rectangle-btn[aria-label="Download PDF"]'
            ),
            download_unavailable_selector=(
                'button.omni-article-tool__check-access'
            ),
        )
    if is_public_or_osu_proxy_host(host, "auajournals.org"):
        return PublisherClickRule(
            publisher="AUA Journals",
            pdf_selector=(
                'a.main-link[aria-label="PDF"][href*="/doi/epdf/"], '
                'a[aria-label="PDF"][href*="/doi/epdf/"], '
                'a[href*="/doi/epdf/"]'
            ),
        )
    if is_public_or_osu_proxy_host(host, "nature.com"):
        return PublisherClickRule(
            publisher="Nature",
            pdf_selector=(
                'a[data-test="download-pdf"][href$=".pdf"], '
                'a[data-article-pdf="true"][href$=".pdf"], '
                'a.c-pdf-download__link[href$=".pdf"], '
                'a[href*="/articles/"][href$=".pdf"]'
            ),
        )
    if is_public_or_osu_proxy_host(host, "haematologica.org"):
        return PublisherClickRule(
            publisher="Haematologica",
            pdf_selector=(
                'a.galley-link.obj_galley_link.pdf[href*="/article/view/"], '
                'a.obj_galley_link.pdf[href*="/article/view/"]'
            ),
        )
    if is_public_or_osu_proxy_host(host, "e-crt.org"):
        return PublisherClickRule(
            publisher="Cancer Research and Treatment",
            pdf_selector=(
                'p.download a[onclick*="journal_download"][onclick*="\'pdf\'"], '
                'a[onclick*="journal_download"][onclick*="\'pdf\'"]'
            ),
        )
    if is_public_or_osu_proxy_host(host, "bmj.com"):
        return PublisherClickRule(
            publisher="BMJ",
            pdf_selector=(
                'div[data-testid="pdf"] '
                'a[data-testid="pdf-button-link"]'
                '[href*="/content/"][href$=".full.pdf"], '
                'a[data-testid="pdf-button-link"]'
                '[href*="/content/"][href$=".full.pdf"]'
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
    download_unavailable_selector: str | None = None,
    download_unavailable_texts: tuple[str, ...] = (),
    direct_navigation: bool = False,
    download_unavailable_statuses: tuple[int, ...] = (),
    download_ready_selector: str | None = None,
) -> PublisherOpenResult:
    """Open a publisher article and click through to its final PDF download."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return PublisherOpenResult(
            PublisherOpenStatus.AUTOMATION_FAILED,
            detail="Chrome AppleScript automation is unavailable",
        )

    attempts = max(1, int(PUBLISHER_CLICK_TIMEOUT_SECONDS / 0.5))
    javascript = r"""
(() => {
  const downloadSelector = __DOWNLOAD_SELECTOR__;
  const revealDownloadSelector = __REVEAL_DOWNLOAD_SELECTOR__;
  const dismissSelector = __DISMISS_SELECTOR__;
  const requestErrorSelector = __REQUEST_ERROR_SELECTOR__;
  const requestErrorText = __REQUEST_ERROR_TEXT__;
  const downloadUnavailableSelector = __DOWNLOAD_UNAVAILABLE_SELECTOR__;
  const downloadUnavailableTexts = __DOWNLOAD_UNAVAILABLE_TEXTS__;
  const pdfSelector = __PDF_SELECTOR__;
  const directNavigation = __DIRECT_NAVIGATION__;
  const downloadUnavailableStatuses = __DOWNLOAD_UNAVAILABLE_STATUSES__;
  const downloadReadySelector = __DOWNLOAD_READY_SELECTOR__;
  const navigationEntry = performance.getEntriesByType('navigation')[0];
  const responseStatus = Number(navigationEntry?.responseStatus || 0);
  if (responseStatus >= 400) return `http_error:${responseStatus}`;
  if (requestErrorSelector && requestErrorText) {
    const requestError = document.querySelector(requestErrorSelector);
    const normalizedErrorText = requestError?.textContent
      ?.replace(/\s+/g, ' ')
      .trim();
    if (normalizedErrorText?.includes(requestErrorText)) {
      return 'request_blocked';
    }
  }

  if (downloadUnavailableStatuses.length > 0) {
    if (downloadUnavailableStatuses.includes(responseStatus)) {
      return 'download_unavailable';
    }
  }

  if (downloadUnavailableSelector) {
    const unavailableMessages = [
      ...document.querySelectorAll(downloadUnavailableSelector),
    ];
    const unavailable = unavailableMessages.some((message) => {
      const messageText = message.textContent?.replace(/\s+/g, ' ').trim()
        .toLocaleLowerCase();
      const selectorOnlyUnavailable = downloadUnavailableTexts.length === 0 &&
        !document.querySelector(pdfSelector);
      return messageText && (
        selectorOnlyUnavailable ||
        downloadUnavailableTexts.some((expectedText) =>
          messageText.includes(expectedText.toLocaleLowerCase())
        )
      );
    });
    if (unavailable) return 'download_unavailable';
  }

  if (directNavigation) {
    if (document.readyState !== 'complete') return 'waiting';
    window.__automatedAccessCheckReadyAt ??= Date.now();
    const accessCheckSettled =
      Date.now() - window.__automatedAccessCheckReadyAt >= 4000;
    return accessCheckSettled ? 'navigation_complete' : 'waiting';
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
    const downloadReadyElement = downloadReadySelector
      ? document.querySelector(downloadReadySelector)
      : null;
    const downloadIsReady = !downloadReadySelector || Boolean(
      downloadReadyElement?.value
    );
    if (downloadLink && downloadIsReady) {
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

  const link = document.querySelector(pdfSelector);
  if (!link) return 'waiting';
  if (link.dataset.automatedPdfClick === 'true') return 'waiting';
  link.dataset.automatedPdfClick = 'true';
  const legacyScienceDirectLink = link.closest('li.ViewPDF') !== null;
  const wileyEpdfLink = link.matches(
    'a.pdf-download[href*="/doi/epdf/"], a[title="ePDF"][href*="/doi/epdf/"]'
  );
  if (wileyEpdfLink) return `navigate_epdf:${link.href}`;
  const bmjPdfLink = link.matches(
    'a[data-testid="pdf-button-link"][href*="/content/"][href$=".full.pdf"]'
  );
  if (bmjPdfLink) {
    const bmjPdfHref = link.getAttribute('href');
    const bmjPdfUrl = new URL(bmjPdfHref, window.location.origin).href;
    return `navigate_bmj_pdf:${bmjPdfUrl}`;
  }
  if (!legacyScienceDirectLink) link.target = '_self';
  link.click();
  if (legacyScienceDirectLink) return 'download_clicked';
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
        "__DOWNLOAD_UNAVAILABLE_SELECTOR__",
        json.dumps(download_unavailable_selector),
    ).replace(
        "__DOWNLOAD_UNAVAILABLE_TEXTS__",
        json.dumps(download_unavailable_texts),
    ).replace(
        "__DIRECT_NAVIGATION__",
        json.dumps(direct_navigation),
    ).replace(
        "__DOWNLOAD_UNAVAILABLE_STATUSES__",
        json.dumps(download_unavailable_statuses),
    ).replace(
        "__DOWNLOAD_READY_SELECTOR__",
        json.dumps(download_ready_selector),
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
        set directNavigation to {str(direct_navigation).lower()}
        set pdfPageOpened to false
        set popupDismissed to false
        set monitoredDownloadChecksRemaining to 0
        set lastJavaScriptError to ""

        repeat {attempts} times
            delay 0.5
            try
                set clickResult to execute articleTab javascript clickScript
                if clickResult is "request_blocked" then return "request_blocked"
                if clickResult is "download_unavailable" then return "download_unavailable"
                if clickResult is "navigation_complete" then return "navigation_complete"
                if clickResult starts with "navigate_epdf:" then
                    set epdfUrl to text 15 thru -1 of clickResult
                    set URL of articleTab to epdfUrl
                    set pdfPageOpened to true
                end if
                if clickResult starts with "navigate_bmj_pdf:" then
                    set bmjPdfUrl to text 18 thru -1 of clickResult
                    set URL of articleTab to bmjPdfUrl
                    return "download_clicked"
                end if
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
                if directNavigation then
                    try
                        set articleUrl to URL of articleTab
                    on error
                        return "navigation_started"
                    end try
                end if
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
        print(
            f"  [AUTO-PDF] {publisher} status check timed out; "
            "the PDF click may already have started"
        )
        return PublisherOpenResult(
            PublisherOpenStatus.AUTOMATION_FAILED,
            detail="publisher click status check timed out",
        )
    except OSError as exc:
        print(f"  [AUTO-PDF] Chrome automation unavailable: {exc}")
        return PublisherOpenResult(
            PublisherOpenStatus.AUTOMATION_FAILED,
            detail=f"Chrome automation unavailable: {exc}",
        )

    status = result.stdout.strip()
    if status.startswith("http_error:"):
        try:
            http_status = int(status.partition(":")[2])
        except ValueError:
            http_status = 0
        print(f"  [AUTO-PDF] {publisher} returned HTTP {http_status}", flush=True)
        result_status = (
            PublisherOpenStatus.DOWNLOAD_UNAVAILABLE
            if http_status in download_unavailable_statuses
            else PublisherOpenStatus.HTTP_ERROR
        )
        return PublisherOpenResult(
            result_status,
            http_status=http_status or None,
            detail=f"top-level browser navigation returned HTTP {http_status}",
        )

    if status == "request_blocked":
        print(
            f"  [AUTO-PDF] detected {publisher} request-limit error page",
            flush=True,
        )
        return PublisherOpenResult(
            PublisherOpenStatus.REQUEST_BLOCKED,
            detail=request_error_text or "publisher request-block page detected",
        )

    if status == "download_unavailable":
        print(
            f"  [AUTO-PDF] {publisher} reports that PDF downloading is unavailable",
            flush=True,
        )
        return PublisherOpenResult(
            PublisherOpenStatus.DOWNLOAD_UNAVAILABLE,
            detail="publisher page reports that PDF downloading is unavailable",
        )

    if status == "navigation_complete":
        print(f"  [AUTO-PDF] checked {publisher} access state")
        return PublisherOpenResult(PublisherOpenStatus.OPENED)

    if status == "navigation_started":
        print(f"  [AUTO-PDF] {publisher} direct PDF request started")
        return PublisherOpenResult(PublisherOpenStatus.OPENED)

    if status in {"download_clicked", "download_clicked_after_popup"}:
        if status == "download_clicked_after_popup":
            print(f"  [AUTO-PDF] dismissed {publisher} popup")
        print(f"  [AUTO-PDF] clicked {publisher} PDF download")
        return PublisherOpenResult(PublisherOpenStatus.OPENED)

    if status.startswith("javascript_error:"):
        print("  [AUTO-PDF] article opened, but Chrome blocked the automatic click")
        print("  [AUTO-PDF] enable View > Developer > Allow JavaScript from Apple Events")
        return PublisherOpenResult(
            PublisherOpenStatus.OPENED,
            detail="Chrome blocked JavaScript from Apple Events",
        )

    if status == "view_pdf_not_found":
        print(f"  [AUTO-PDF] {publisher} PDF link was not found; article left open")
        return PublisherOpenResult(
            PublisherOpenStatus.OPENED,
            detail="publisher PDF link was not found",
        )

    if status == "download_link_not_found":
        print(f"  [AUTO-PDF] {publisher} PDF opened, but its download link was not found")
        return PublisherOpenResult(
            PublisherOpenStatus.OPENED,
            detail="PDF page opened but its download link was not found",
        )

    error = result.stderr.strip() or status or f"osascript exited {result.returncode}"
    print(f"  [AUTO-PDF] Chrome automation failed: {error}")
    return PublisherOpenResult(
        PublisherOpenStatus.AUTOMATION_FAILED,
        detail=error,
    )


def open_url(url: str) -> PublisherOpenResult:
    if OPEN_COMMAND:
        subprocess.run([*shlex.split(OPEN_COMMAND), url], check=False)
        return PublisherOpenResult(PublisherOpenStatus.OPENED)

    if AUTOMATE_PUBLISHER_PDF_CLICK:
        strategy = download_strategy(url)
        click_rule = publisher_pdf_click_rule(url)
        if click_rule and (strategy.automatic or click_rule.direct_navigation):
            open_status = open_publisher_and_click_pdf(
                url,
                click_rule.publisher,
                click_rule.pdf_selector,
                click_rule.download_selector,
                click_rule.reveal_download_selector,
                click_rule.dismiss_selector,
                click_rule.request_error_selector,
                click_rule.request_error_text,
                click_rule.download_unavailable_selector,
                click_rule.download_unavailable_texts,
                click_rule.direct_navigation,
                click_rule.download_unavailable_statuses,
                click_rule.download_ready_selector,
            )
            if open_status.status is not PublisherOpenStatus.AUTOMATION_FAILED:
                return open_status

    webbrowser.open_new_tab(url)
    return PublisherOpenResult(PublisherOpenStatus.OPENED)


def existing_pdf_path(pmid: str) -> Path | None:
    candidate = OUTPUT_DIR / f"{pmid}.pdf"
    return candidate if candidate.is_file() and is_pdf_file(candidate) else None


def now_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def publisher_name_for_url(url: str) -> str:
    click_rule = publisher_pdf_click_rule(url)
    if click_rule:
        return click_rule.publisher
    return (urlparse(url).hostname or "unknown publisher").casefold()


def failure_for_http_status(http_status: int, detail: str = "") -> FailureDetails:
    descriptions = {
        401: ("authentication", False, "refresh institutional login, then retry manually"),
        403: ("access_denied", False, "pause this publisher and review access or blocking"),
        404: ("not_found", False, "verify this article URL or DOI manually"),
        429: ("rate_limited", True, "pause this publisher before retrying"),
        502: ("publisher_server_error", True, "pause and retry this publisher later"),
        503: ("publisher_unavailable", True, "pause and retry this publisher later"),
        504: ("publisher_timeout", True, "pause and retry this publisher later"),
    }
    category, retryable, action = descriptions.get(
        http_status,
        (
            "http_client_error" if 400 <= http_status < 500 else "http_server_error",
            http_status >= 500,
            "review the publisher response before retrying",
        ),
    )
    return FailureDetails(
        category=category,
        code=f"http_{http_status}",
        detail=detail or f"publisher returned HTTP {http_status}",
        retryable=retryable,
        recommended_action=action,
    )


def should_auto_skip_timeout(publisher: str) -> bool:
    exceptions = {name.casefold() for name in AUTO_SKIP_PUBLISHER_EXCEPTIONS}
    return AUTO_SKIP_MODE and publisher.casefold() not in exceptions


def circuit_signal_for_failure(failure: FailureDetails) -> str:
    return failure.code if failure.code in PUBLISHER_CIRCUIT_THRESHOLDS else ""


def record_publisher_failure(
    publisher: str,
    failure: FailureDetails,
    consecutive_failures: dict[str, tuple[str, int]],
) -> PublisherCircuit | None:
    """Count only consecutive, explicit block/HTTP signals for one publisher."""
    signal = circuit_signal_for_failure(failure)
    if not signal:
        consecutive_failures.pop(publisher, None)
        return None

    previous_signal, previous_count = consecutive_failures.get(publisher, ("", 0))
    count = previous_count + 1 if previous_signal == signal else 1
    consecutive_failures[publisher] = (signal, count)
    threshold = PUBLISHER_CIRCUIT_THRESHOLDS[signal]
    if count < threshold:
        return None
    return PublisherCircuit(
        publisher=publisher,
        signal=signal,
        failure_count=count,
        threshold=threshold,
        opened_at=now_timestamp(),
    )


def reset_publisher_failures(
    publisher: str,
    consecutive_failures: dict[str, tuple[str, int]],
) -> None:
    consecutive_failures.pop(publisher, None)


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
    publisher: str = "",
    failure: FailureDetails | None = None,
    publisher_failure_count: int = 0,
) -> None:
    """Write a complete result state, clearing fields from any prior attempt."""
    row.update(
        {
            FETCH_STATUS_COLUMN: status,
            FETCH_SOURCE_COLUMN: source,
            FETCH_ERROR_COLUMN: error,
            FETCH_ERROR_CATEGORY_COLUMN: failure.category if failure else "",
            FETCH_ERROR_CODE_COLUMN: failure.code if failure else "",
            FETCH_ERROR_DETAIL_COLUMN: failure.detail if failure else "",
            FETCH_ERROR_RETRYABLE_COLUMN: (
                "Y" if failure and failure.retryable else "N" if failure else ""
            ),
            FETCH_ERROR_ACTION_COLUMN: failure.recommended_action if failure else "",
            FETCH_PUBLISHER_COLUMN: publisher,
            FETCH_PUBLISHER_FAILURE_COUNT_COLUMN: (
                str(publisher_failure_count) if publisher_failure_count else ""
            ),
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
            failure = FailureDetails(
                category="validation",
                code="invalid_pmid",
                detail=f"PMID must contain ASCII digits only: {raw_pmid!r}",
                retryable=False,
                recommended_action="correct the PMID in the input CSV",
            )
            log_event(
                {
                    "event": "failed",
                    "pmid": raw_pmid,
                    "error": failure.__dict__,
                    "finished_at": now_timestamp(),
                }
            )
            write_result(
                row,
                status="failed",
                source="validation",
                error=failure.code,
                failure=failure,
            )
            continue
        if pmid in seen_pmids:
            failure = FailureDetails(
                category="validation",
                code="duplicate_pmid",
                detail=f"PMID {pmid} appears more than once in this input batch",
                retryable=False,
                recommended_action="remove or merge the duplicate CSV row",
            )
            log_event(
                {
                    "event": "skipped",
                    "pmid": pmid,
                    "error": failure.__dict__,
                    "finished_at": now_timestamp(),
                }
            )
            write_result(
                row,
                status="skipped",
                source="validation",
                error=failure.code,
                failure=failure,
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
            failure = FailureDetails(
                category="validation",
                code="invalid_doi",
                detail=f"DOI is not a DOI value or HTTP(S) URL: {doi!r}",
                retryable=False,
                recommended_action="correct the DOI in the input CSV",
            )
            log_event(
                {
                    "event": "failed",
                    "pmid": pmid,
                    "doi": doi,
                    "error": failure.__dict__,
                    "finished_at": now_timestamp(),
                }
            )
            write_result(
                row,
                status="failed",
                source="validation",
                error=failure.code,
                failure=failure,
            )
            continue

        eligible.append((row, pmid, doi))

    selected = eligible[BATCH_START:BATCH_START + BATCH_LIMIT]
    selected_count = len(selected)
    print(
        f"[URL PREP] selected={selected_count} eligible={len(eligible)} "
        f"start={BATCH_START} limit={BATCH_LIMIT} "
        f"override_existing={OVERRIDE_EXISTING_DOWNLOAD_URLS} local_only=True"
    )

    queue: list[dict[str, str]] = []
    action_counts: dict[str, int] = {}
    blocked_count = 0
    for index, (row, pmid, doi) in enumerate(selected, start=1):
        existing_url = normalize_cell(row.get(DOWNLOAD_URL_COLUMN))
        override_url = institutional_url_override(doi)
        if override_url:
            planned_action = "institutional override"
        elif OVERRIDE_EXISTING_DOWNLOAD_URLS and existing_url:
            planned_action = "refresh existing URL"
        elif OVERRIDE_EXISTING_DOWNLOAD_URLS:
            planned_action = "resolve DOI"
        elif is_http_url(existing_url):
            planned_action = "reuse existing URL"
        else:
            planned_action = "resolve DOI"
        print(
            f"[URL {index}/{selected_count}] pmid {pmid} | {planned_action}"
        )

        if override_url:
            url = override_url
            action = "override"
            if url != existing_url:
                print(f"  [OVERRIDE] doi={doi} -> {url}")
        elif OVERRIDE_EXISTING_DOWNLOAD_URLS:
            repaired_url = (
                rewrite_resolved_url(existing_url, fallback_doi=doi)
                if is_http_url(existing_url)
                else existing_url
            )
            if repaired_url != existing_url:
                url = repaired_url
                action = "repaired"
                print(f"  [REPAIR URL] {existing_url} -> {url}")
            else:
                refreshed_url = prepare_pdf_url_locally(doi)
                url = refreshed_url
                action = (
                    "lazy_doi_resolution"
                    if is_doi_resolver_url(url)
                    else "refreshed_locally"
                )
                if existing_url and url != existing_url:
                    print(f"  [REFRESH URL] {existing_url} -> {url}")
        elif is_http_url(existing_url):
            url = existing_url
            action = "reused"
        else:
            url = prepare_pdf_url_locally(doi)
            action = (
                "lazy_doi_resolution"
                if is_doi_resolver_url(url)
                else "prepared_locally"
            )

        action_counts[action] = action_counts.get(action, 0) + 1

        blocked_pattern = matching_blocked_pattern(url)
        if blocked_pattern:
            blocked_count += 1
            print(f"  [SKIP URL] pmid={pmid} pattern={blocked_pattern} url={url}")
            failure = FailureDetails(
                category="configured_skip",
                code="blocked_url_pattern",
                detail=f"URL matched configured blocked pattern {blocked_pattern!r}",
                retryable=False,
                recommended_action="review manually or revise BLOCKED_URL_PATTERNS",
            )
            log_event(
                {
                    "event": "skipped",
                    "pmid": pmid,
                    "publisher": publisher_name_for_url(url),
                    "url": url,
                    "matched_pattern": blocked_pattern,
                    "error": failure.__dict__,
                    "finished_at": now_timestamp(),
                }
            )
            write_result(
                row,
                status="skipped",
                source="skip_rule",
                error=f"blocked_url_pattern:{blocked_pattern}",
                url=url,
                publisher=publisher_name_for_url(url),
                failure=failure,
            )
            continue

        write_result(row, status="pending", url=url)
        queue.append(row)

    queue.sort(
        key=lambda queued_row: download_strategy(
            normalize_cell(queued_row.get(DOWNLOAD_URL_COLUMN))
        ).priority
    )
    action_summary = " ".join(
        f"{action}={count}" for action, count in sorted(action_counts.items())
    )
    print(
        f"[URL PREP DONE] queued={len(queue)} blocked={blocked_count}"
        + (f" | {action_summary}" if action_summary else "")
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
    if (
        DOI_RESOLUTION_MIN_INTERVAL_SECONDS < 0
        or DOI_RESOLUTION_JITTER_SECONDS < 0
        or DOI_RESOLUTION_DEFAULT_RATE_LIMIT_PAUSE_SECONDS < 0
        or DOI_RESOLUTION_MAX_RETRY_AFTER_SECONDS < 0
    ):
        print("DOI resolution pacing and cooldown values must be >= 0")
        return 1
    if any(threshold <= 0 for threshold in PUBLISHER_CIRCUIT_THRESHOLDS.values()):
        print("All publisher circuit thresholds must be > 0")
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
    timeout_mode = "auto skip" if AUTO_SKIP_MODE else "interactive retry/skip"
    print(
        f"[TIMEOUT MODE] {timeout_mode}; wait={WAIT_TIMEOUT_SECONDS}s; "
        f"exceptions={sorted(AUTO_SKIP_PUBLISHER_EXCEPTIONS) or 'none'}"
    )
    if BATCH_LOG_PATH is not None:
        print(f"[EVENT LOG] {BATCH_LOG_PATH}")
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
    consecutive_publisher_failures: dict[str, tuple[str, int]] = {}
    open_publisher_circuits: dict[str, PublisherCircuit] = {}

    for position, row in enumerate(queue, start=1):
        pmid = normalize_pmid(row.get(PMID_COLUMN))
        url = normalize_cell(row.get(DOWNLOAD_URL_COLUMN))
        destination = OUTPUT_DIR / f"{pmid}.pdf"

        print(f"\n[{position}/{len(queue)}] pmid={pmid}")

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

        provisional_publisher = publisher_name_for_url(url)
        if (
            not DRY_RUN
            and is_doi_resolver_url(url)
            and provisional_publisher not in open_publisher_circuits
        ):
            print(f"  [LAZY RESOLVE] resolving DOI immediately before this record")
            resolved_url = resolve_best_pdf_url(normalize_doi(row.get(DOI_COLUMN)))
            if resolved_url:
                url = resolved_url
                row[DOWNLOAD_URL_COLUMN] = url
                # Cache the result immediately so interrupted runs do not repeat
                # successful DOI resolution work.
                write_csv_rows(OUTPUT_CSV, fieldnames, rows)

        blocked_pattern = matching_blocked_pattern(url)
        if blocked_pattern:
            finished_at = now_timestamp()
            failure = FailureDetails(
                category="configured_skip",
                code="blocked_url_pattern",
                detail=(
                    f"lazily resolved URL matched configured blocked pattern "
                    f"{blocked_pattern!r}"
                ),
                retryable=False,
                recommended_action="review manually or revise BLOCKED_URL_PATTERNS",
            )
            publisher = publisher_name_for_url(url)
            print(
                f"  [SKIP URL] lazy result matched pattern={blocked_pattern} url={url}"
            )
            log_event(
                {
                    "event": "skipped",
                    "pmid": pmid,
                    "publisher": publisher,
                    "url": url,
                    "matched_pattern": blocked_pattern,
                    "error": failure.__dict__,
                    "finished_at": finished_at,
                }
            )
            write_result(
                row,
                status="skipped",
                source="skip_rule",
                error=f"blocked_url_pattern:{blocked_pattern}",
                finished_at=finished_at,
                url=url,
                publisher=publisher,
                failure=failure,
            )
            write_csv_rows(OUTPUT_CSV, fieldnames, rows)
            continue

        strategy = download_strategy(url)
        publisher = publisher_name_for_url(url)
        row[FETCH_PUBLISHER_COLUMN] = publisher
        mode = "automatic" if strategy.automatic else "manual review"
        print(f"  [MODE] {mode}: {strategy.label}")
        print(f"  URL: {url}")
        if not strategy.automatic:
            print("  [ACTION] complete any login, access, or PDF clicks in the browser")

        circuit = open_publisher_circuits.get(publisher)
        if circuit:
            finished_at = now_timestamp()
            failure = FailureDetails(
                category="publisher_circuit_open",
                code=circuit.signal,
                detail=(
                    f"publisher skipped after {circuit.failure_count} consecutive "
                    f"{circuit.signal} failures (threshold {circuit.threshold})"
                ),
                retryable=True,
                recommended_action="pause this publisher and retry later or review manually",
            )
            print(
                f"  [PUBLISHER SKIP] {publisher}: circuit open after "
                f"{circuit.failure_count} consecutive {circuit.signal} failures"
            )
            log_event(
                {
                    "event": "publisher_auto_skipped",
                    "pmid": pmid,
                    "publisher": publisher,
                    "url": url,
                    "error": failure.__dict__,
                    "publisher_failure_count": circuit.failure_count,
                    "circuit_opened_at": circuit.opened_at,
                    "finished_at": finished_at,
                }
            )
            write_result(
                row,
                status="skipped",
                source="publisher_circuit_breaker",
                error="publisher_circuit_open",
                finished_at=finished_at,
                url=url,
                publisher=publisher,
                failure=failure,
                publisher_failure_count=circuit.failure_count,
            )
            write_csv_rows(OUTPUT_CSV, fieldnames, rows)
            continue

        known = snapshot_completed_files(WATCH_DIR)
        started_at = now_timestamp()
        log_event(
            {
                "event": "open",
                "pmid": pmid,
                "publisher": publisher,
                "url": url,
                "started_at": started_at,
            }
        )

        request_error_failure = ""
        download_unavailable = False
        if not DRY_RUN:
            request_error_retries = 0
            while True:
                open_status = open_url(url)
                time.sleep(OPEN_DELAY_SECONDS)
                if open_status.status is PublisherOpenStatus.DOWNLOAD_UNAVAILABLE:
                    download_unavailable = True
                    break
                if open_status.status is PublisherOpenStatus.HTTP_ERROR:
                    break
                if open_status.status is not PublisherOpenStatus.REQUEST_BLOCKED:
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

        if open_status.status is PublisherOpenStatus.HTTP_ERROR:
            finished_at = now_timestamp()
            failure = failure_for_http_status(
                open_status.http_status or 0,
                open_status.detail,
            )
            circuit = record_publisher_failure(
                publisher,
                failure,
                consecutive_publisher_failures,
            )
            failure_count = consecutive_publisher_failures.get(publisher, ("", 0))[1]
            if circuit:
                open_publisher_circuits[publisher] = circuit
                print(
                    f"  [CIRCUIT OPEN] {publisher} reached {failure_count} "
                    f"consecutive {failure.code} failures; later records will be skipped"
                )
            log_event(
                {
                    "event": "failed",
                    "pmid": pmid,
                    "publisher": publisher,
                    "url": url,
                    "http_status": open_status.http_status,
                    "error": failure.__dict__,
                    "publisher_failure_count": failure_count,
                    "circuit_opened": bool(circuit),
                    "started_at": started_at,
                    "finished_at": finished_at,
                }
            )
            write_result(
                row,
                status="failed",
                error=failure.code,
                started_at=started_at,
                finished_at=finished_at,
                url=url,
                publisher=publisher,
                failure=failure,
                publisher_failure_count=failure_count,
            )
            write_csv_rows(OUTPUT_CSV, fieldnames, rows)
            continue

        if download_unavailable:
            finished_at = now_timestamp()
            failure = (
                failure_for_http_status(open_status.http_status, open_status.detail)
                if open_status.http_status
                else FailureDetails(
                    category="access_unavailable",
                    code="publisher_download_unavailable",
                    detail=open_status.detail,
                    retryable=False,
                    recommended_action="review access for this article manually",
                )
            )
            reason = failure.code
            record_publisher_failure(
                publisher,
                failure,
                consecutive_publisher_failures,
            )
            print("  [SKIP] publisher does not permit this PDF download")
            log_event(
                {
                    "event": "skipped",
                    "pmid": pmid,
                    "publisher": publisher,
                    "url": url,
                    "reason": reason,
                    "http_status": open_status.http_status,
                    "error": failure.__dict__,
                    "started_at": started_at,
                    "finished_at": finished_at,
                }
            )
            write_result(
                row,
                status="skipped",
                error=reason,
                started_at=started_at,
                finished_at=finished_at,
                url=url,
                publisher=publisher,
                failure=failure,
            )
            write_csv_rows(OUTPUT_CSV, fieldnames, rows)
            click_rule = publisher_pdf_click_rule(url)
            should_close_tab = strategy.automatic or bool(
                click_rule and click_rule.direct_navigation
            )
            if CLOSE_COMPLETED_AUTOMATIC_TABS and should_close_tab:
                close_completed_automatic_tab(url)
            continue

        if request_error_failure:
            finished_at = now_timestamp()
            failure = FailureDetails(
                category="rate_limited",
                code="request_blocked",
                detail=(
                    f"{request_error_failure}; publisher block page remained after "
                    f"{request_error_retries} recovery retries"
                ),
                retryable=True,
                recommended_action="pause this publisher before retrying",
            )
            circuit = record_publisher_failure(
                publisher,
                failure,
                consecutive_publisher_failures,
            )
            failure_count = consecutive_publisher_failures.get(publisher, ("", 0))[1]
            if circuit:
                open_publisher_circuits[publisher] = circuit
                print(
                    f"  [CIRCUIT OPEN] {publisher} reached {failure_count} "
                    "consecutive request-blocked failures; later records will be skipped"
                )
            print(
                "  [FAILED] ScienceDirect request-error recovery stopped: "
                f"{request_error_failure}",
                flush=True,
            )
            log_event(
                {
                    "event": "failed",
                    "pmid": pmid,
                    "publisher": publisher,
                    "url": url,
                    "reason": request_error_failure,
                    "error": failure.__dict__,
                    "publisher_failure_count": failure_count,
                    "circuit_opened": bool(circuit),
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
                publisher=publisher,
                failure=failure,
                publisher_failure_count=failure_count,
            )
            write_csv_rows(OUTPUT_CSV, fieldnames, rows)
            continue

        while True:
            skip_keys = "'s'" if os.name == "nt" else "'s' + Enter"
            hint = f"| press {skip_keys} to skip" if INTERACTIVE_SKIP else ""
            print(f"  [WAIT] {WAIT_TIMEOUT_SECONDS}s {hint}".rstrip())
            new_file, skipped = wait_for_download(known, expected_url=url)

            if skipped:
                finished_at = now_timestamp()
                failure = FailureDetails(
                    category="user_action",
                    code="manually_skipped",
                    detail="user skipped the record while waiting for a download",
                    retryable=True,
                    recommended_action="retry manually if the PDF is still needed",
                )
                print("  [SKIP] manually skipped")
                reset_publisher_failures(publisher, consecutive_publisher_failures)
                log_event(
                    {
                        "event": "skipped",
                        "pmid": pmid,
                        "publisher": publisher,
                        "url": url,
                        "reason": "manually_skipped",
                        "error": failure.__dict__,
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
                    publisher=publisher,
                    failure=failure,
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
                        "publisher": publisher,
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
                    publisher=publisher,
                )
                write_csv_rows(OUTPUT_CSV, fieldnames, rows)
                reset_publisher_failures(publisher, consecutive_publisher_failures)
                click_rule = publisher_pdf_click_rule(url)
                uses_transient_download_tab = bool(
                    click_rule and click_rule.direct_navigation
                )
                if (
                    CLOSE_COMPLETED_AUTOMATIC_TABS
                    and strategy.automatic
                    and not uses_transient_download_tab
                ):
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
            auto_skip = should_auto_skip_timeout(publisher)
            if INTERACTIVE_SKIP and not auto_skip:
                choice = input("  Retry (r) or skip (s)? ").strip().lower()
                if choice == "r":
                    continue

            finished_at = now_timestamp()
            failure = FailureDetails(
                category="download_timeout",
                code="download_not_detected",
                detail=(
                    f"no valid PDF appeared in {WATCH_DIR} within "
                    f"{WAIT_TIMEOUT_SECONDS}s; strategy={strategy.label}; "
                    f"automation_detail={open_status.detail or 'none'}"
                ),
                retryable=True,
                recommended_action="review the page manually or retry this article later",
            )
            final_status = "skipped" if auto_skip else "failed"
            event = "auto_skipped" if auto_skip else "failed"
            reset_publisher_failures(publisher, consecutive_publisher_failures)
            if auto_skip:
                print(f"  [AUTO SKIP] no download after {WAIT_TIMEOUT_SECONDS}s")
            log_event(
                {
                    "event": event,
                    "pmid": pmid,
                    "publisher": publisher,
                    "url": url,
                    "reason": failure.code,
                    "error": failure.__dict__,
                    "wait_timeout_seconds": WAIT_TIMEOUT_SECONDS,
                    "strategy": strategy.label,
                    "automatic_strategy": strategy.automatic,
                    "auto_skip_mode": AUTO_SKIP_MODE,
                    "started_at": started_at,
                    "finished_at": finished_at,
                }
            )
            write_result(
                row,
                status=final_status,
                source="auto_skip" if auto_skip else "browser",
                error=failure.code,
                started_at=started_at,
                finished_at=finished_at,
                url=url,
                publisher=publisher,
                failure=failure,
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
