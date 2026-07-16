"""Open DOI-based URLs in a browser and rename downloaded PDFs."""

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
from pathlib import Path
from urllib.parse import urlparse

import requests


PROJECT_ROOT = Path(__file__).resolve().parent

DOWNLOADS_DIR = (PROJECT_ROOT / "downloads").resolve()
UNPAYWALL_PDF_DIR = (DOWNLOADS_DIR / "unpaywall").resolve()
BROWSER_PDF_DIR = (PROJECT_ROOT / "browser").resolve()
BROWSER_WATCH_DIR = (Path.home() / "Downloads").resolve()

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

EXISTING_PDF_RULES = [
    {
        "directory": BROWSER_PDF_DIR,
        "filename_template": "{pmid}.pdf",
    },
]

def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_cell(value: str | None) -> str:
    return (value or "").strip()


def is_yes(value: str | None) -> bool:
    return normalize_cell(value).upper() == TARGET_ENABLED_VALUE


def ensure_columns(fieldnames: list[str], required_columns: list[str]) -> list[str]:
    merged = list(fieldnames)
    for column in required_columns:
        if column not in merged:
            merged.append(column)
    return merged


def load_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, path)


INPUT_CSV = (PROJECT_ROOT / "target_records.csv").resolve()
OUTPUT_CSV = INPUT_CSV
WATCH_DIR = BROWSER_WATCH_DIR
OUTPUT_DIR = BROWSER_PDF_DIR

BATCH_START = 0
BATCH_LIMIT = 1000
OPEN_DELAY_SECONDS = 0.5
WAIT_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 1.0
MIN_FILE_SIZE_BYTES = 1024
INTERACTIVE_SKIP = True
DRY_RUN = False
OPEN_COMMAND: str | None = None
BATCH_LOG_PATH: Path | None = None
BLOCKED_URL_PATTERNS = ["karger.com"]
AUTOMATE_PUBLISHER_PDF_CLICK = True
PUBLISHER_CLICK_TIMEOUT_SECONDS = 30

SCIENCEDIRECT_PROXY_HOST = "www-sciencedirect-com.proxy.lib.ohio-state.edu"
SCIENCEDIRECT_ARTICLE_BASE_URL = (
    f"https://{SCIENCEDIRECT_PROXY_HOST}/science/article/pii"
)
ACS_PROXY_HOST = "pubs-acs-org.proxy.lib.ohio-state.edu"
ACS_ARTICLE_BASE_URL = f"https://{ACS_PROXY_HOST}/doi/full"
WILEY_PROXY_HOST = (
    "analyticalsciencejournals-onlinelibrary-wiley-com.proxy.lib.ohio-state.edu"
)
WILEY_ARTICLE_BASE_URL = f"https://{WILEY_PROXY_HOST}/doi/full"
SAGE_PROXY_HOST = "journals-sagepub-com.proxy.lib.ohio-state.edu"
SAGE_ARTICLE_BASE_URL = f"https://{SAGE_PROXY_HOST}/doi/full"

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
    ("10.1089/", f"{SAGE_ARTICLE_BASE_URL}/{{doi}}"),
]


def strip_doi_path_slug(after_doi: str) -> str:
    for slug in ("abs/", "full/", "pdf/", "pdfdirect/", "epdf/", "epub/", "reader/"):
        if after_doi.startswith(slug):
            return after_doi[len(slug):]
    return after_doi


def is_wiley_host(host: str) -> bool:
    return (
        host.endswith("onlinelibrary.wiley.com")
        or host.endswith("onlinelibrary-wiley-com.proxy.lib.ohio-state.edu")
    )


def is_sage_host(host: str) -> bool:
    return host == "journals.sagepub.com" or host == SAGE_PROXY_HOST


def rewrite_resolved_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path

    if is_wiley_host(host) and "/doi/" in path:
        after = strip_doi_path_slug(path.split("/doi/", 1)[1])
        if after.startswith("10."):
            return f"{WILEY_ARTICLE_BASE_URL}/{after.split('?')[0]}"

    if host in ("pubs.acs.org", ACS_PROXY_HOST) and "/doi/" in path:
        after = strip_doi_path_slug(path.split("/doi/", 1)[1])
        if after.startswith("10."):
            return f"{ACS_ARTICLE_BASE_URL}/{after.split('?')[0]}"

    if host.endswith("frontiersin.org"):
        if path.endswith("/full"):
            return url[: -len("/full")] + "/pdf"
        if "/articles/" in path and not path.endswith("/pdf"):
            return url.rstrip("/") + "/pdf"

    if host == "pubs.rsc.org" and "/articlelanding/" in path:
        return url.replace("/articlelanding/", "/articlepdf/")

    if host == "elifesciences.org" and "/articles/" in path:
        article_id = path.split("/articles/")[-1].split("/")[0].split("?")[0]
        if article_id and not article_id.endswith(".pdf"):
            return f"https://elifesciences.org/articles/{article_id}.pdf"

    if host in ("pnas.org", "www.pnas.org") and "/doi/" in path:
        after = strip_doi_path_slug(path.split("/doi/", 1)[1])
        return f"https://www.pnas.org/doi/pdf/{after.split('?')[0]}"

    if host in ("science.org", "www.science.org") and "/doi/" in path:
        after = strip_doi_path_slug(path.split("/doi/", 1)[1])
        return f"https://www.science.org/doi/pdf/{after.split('?')[0]}"

    if host == "journals.asm.org" and "/doi/" in path:
        after = strip_doi_path_slug(path.split("/doi/", 1)[1])
        return f"https://journals.asm.org/doi/pdf/{after.split('?')[0]}"

    if is_sage_host(host) and "/doi/" in path:
        after = strip_doi_path_slug(path.split("/doi/", 1)[1])
        return f"{SAGE_ARTICLE_BASE_URL}/{after.split('?')[0]}"

    if host == "linkinghub.elsevier.com" and "/retrieve/pii/" in path:
        pii = path.split("/retrieve/pii/")[-1].strip("/").split("?")[0]
        if pii:
            return f"{SCIENCEDIRECT_ARTICLE_BASE_URL}/{pii}"

    if host.endswith("sciencedirect.com") and "/article/pii/" in path:
        pii = path.split("/article/pii/")[-1].strip("/").split("?")[0]
        if pii:
            return f"{SCIENCEDIRECT_ARTICLE_BASE_URL}/{pii}"

    if host.endswith("link.springer.com"):
        if path.startswith("/content/pdf/") and path.endswith(".pdf"):
            return url
        if path.startswith("/article/"):
            doi_part = path.replace("/article/", "").strip("/")
            if doi_part:
                return f"https://link.springer.com/content/pdf/{doi_part}.pdf"

    return url


def resolve_best_pdf_url(doi: str, resolve_timeout: float = 8.0) -> str:
    doi = doi.strip()
    if not doi:
        return ""
    if doi.startswith("http"):
        return rewrite_resolved_url(doi)
    if doi.startswith("10.7554/eLife."):
        article_id = doi.split("eLife.")[-1]
        return f"https://elifesciences.org/articles/{article_id}.pdf"

    for prefix, template in DOI_PREFIX_RULES:
        if doi.startswith(prefix):
            return template.format(doi=doi)

    doi_url = f"https://doi.org/{doi}"
    try:
        response = requests.head(
            doi_url,
            allow_redirects=True,
            timeout=resolve_timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; browser-pdf-fetch/1.0)"},
        )
        resolved = response.url
        print(f"  [RESOLVE] {doi_url} -> {resolved}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [RESOLVE] failed ({exc}), falling back to doi.org URL")
        return doi_url

    rewritten = rewrite_resolved_url(resolved)
    if rewritten != resolved:
        print(f"  [REWRITE] -> {rewritten}")
    return rewritten


def is_blocked(url: str) -> bool:
    lowered = url.lower()
    return any(pattern.lower() in lowered for pattern in BLOCKED_URL_PATTERNS)


def completed_files(directory: Path) -> list[Path]:
    results: list[Path] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() in TEMP_SUFFIXES:
            continue
        if any(Path(str(path) + suffix).exists() for suffix in TEMP_SUFFIXES):
            continue
        results.append(path)
    return results


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


def wait_for_download(known: set[Path]) -> tuple[Path | None, bool]:
    deadline = time.time() + WAIT_TIMEOUT_SECONDS
    while time.time() < deadline:
        if INTERACTIVE_SKIP and read_skip_nonblocking():
            return None, True

        candidates = [
            path for path in completed_files(WATCH_DIR)
            if path.resolve() not in known and path.stat().st_size >= MIN_FILE_SIZE_BYTES
        ]
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime), False

        time.sleep(POLL_INTERVAL_SECONDS)

    return None, False


def log_event(payload: dict) -> None:
    if BATCH_LOG_PATH is None:
        return
    BATCH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BATCH_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def publisher_pdf_click_rule(url: str) -> tuple[str, str, str | None] | None:
    host = urlparse(url).netloc.lower().split(":", 1)[0]
    if host.endswith("sciencedirect.com") or host == SCIENCEDIRECT_PROXY_HOST:
        return (
            "ScienceDirect",
            'a[aria-label^="View PDF"][href*="/pdfft"], a[href*="/pdfft"]',
            None,
        )
    if host in ("pubs.acs.org", ACS_PROXY_HOST):
        return (
            "ACS",
            'a[data-id="article_header_OpenPDF"][href*="/doi/pdf/"], '
            'a.article__btn__secondary--pdf[href*="/doi/pdf/"]',
            None,
        )
    if is_wiley_host(host):
        return (
            "Wiley",
            'a.pdf-download[href*="/doi/epdf/"], '
            'a[title="ePDF"][href*="/doi/epdf/"]',
            'a.navbar-download[href*="/doi/pdfdirect/"][data-single-download="true"], '
            'a.navbar-download[href*="/doi/pdfdirect/"], '
            'div.navbar-download a.download[data-download-files-key="pdf"]'
            '[href*="/doi/pdfdirect/"], '
            'a.download[data-download-files-key="pdf"][href*="/doi/pdfdirect/"]',
        )
    if is_sage_host(host):
        return (
            "SAGE",
            'a[data-id="article-toolbar-pdf-epub"][href*="/doi/reader/"], '
            'a[href*="/doi/reader/"]',
            'a#favourite-download[href*="/doi/pdf/"], '
            'a[aria-label="Download PDF"][href*="/doi/pdf/"]',
        )
    return None


def open_publisher_and_click_pdf(
    url: str,
    publisher: str,
    selector: str,
    download_selector: str | None,
) -> bool:
    """Open a publisher article and click through to its final PDF download."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return False

    attempts = max(1, int(PUBLISHER_CLICK_TIMEOUT_SECONDS / 0.5))
    javascript = """
(() => {
  const downloadSelector = __DOWNLOAD_SELECTOR__;
  if (downloadSelector) {
    const downloadLink = document.querySelector(downloadSelector);
    if (downloadLink) {
      downloadLink.target = '_self';
      downloadLink.click();
      return 'download_clicked';
    }
  }

  const link = document.querySelector(__PDF_SELECTOR__);
  if (!link) return 'waiting';
  if (link.dataset.automatedPdfClick === 'true') return 'waiting';
  link.dataset.automatedPdfClick = 'true';
  link.target = '_self';
  link.click();
  return downloadSelector ? 'pdf_page_opened' : 'download_clicked';
})()
""".replace(
        "__DOWNLOAD_SELECTOR__", json.dumps(download_selector)
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
        set lastJavaScriptError to ""

        repeat {attempts} times
            delay 0.5
            try
                set clickResult to execute articleTab javascript clickScript
                if clickResult is "download_clicked" then return "download_clicked"
                if clickResult is "pdf_page_opened" then set pdfPageOpened to true
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
        )
    except OSError as exc:
        print(f"  [AUTO-PDF] Chrome automation unavailable: {exc}")
        return False

    status = result.stdout.strip()
    if status == "download_clicked":
        print(f"  [AUTO-PDF] clicked {publisher} PDF download")
        return True

    if status.startswith("javascript_error:"):
        print("  [AUTO-PDF] article opened, but Chrome blocked the automatic click")
        print("  [AUTO-PDF] enable View > Developer > Allow JavaScript from Apple Events")
        return True

    if status == "view_pdf_not_found":
        print(f"  [AUTO-PDF] {publisher} PDF link was not found; article left open")
        return True

    if status == "download_link_not_found":
        print(f"  [AUTO-PDF] {publisher} PDF opened, but its download link was not found")
        return True

    error = result.stderr.strip() or status or f"osascript exited {result.returncode}"
    print(f"  [AUTO-PDF] Chrome automation failed: {error}")
    return False


def open_url(url: str) -> None:
    if OPEN_COMMAND:
        subprocess.run([*shlex.split(OPEN_COMMAND), url], check=False)
        return

    if AUTOMATE_PUBLISHER_PDF_CLICK:
        click_rule = publisher_pdf_click_rule(url)
        if click_rule:
            publisher, selector, download_selector = click_rule
            if open_publisher_and_click_pdf(
                url, publisher, selector, download_selector
            ):
                return

    webbrowser.open_new_tab(url)


def existing_pdf_path(pmid: str) -> Path | None:
    for rule in EXISTING_PDF_RULES:
        candidate = rule["directory"] / rule["filename_template"].format(pmid=pmid)
        if candidate.is_file() and candidate.stat().st_size >= MIN_FILE_SIZE_BYTES:
            return candidate
    return None


def write_result(row: dict[str, str], *, status: str, error: str = "", started_at: str = "", finished_at: str = "", filename: str = "", url: str = "", output_path: str = "") -> None:
    row[FETCH_STATUS_COLUMN] = status
    row[FETCH_SOURCE_COLUMN] = "browser"
    row[FETCH_ERROR_COLUMN] = error
    if started_at:
        row[DOWNLOAD_STARTED_AT_COLUMN] = started_at
    if finished_at:
        row[DOWNLOAD_FINISHED_AT_COLUMN] = finished_at
    if filename:
        row[DOWNLOAD_FILENAME_COLUMN] = filename
    if url:
        row[DOWNLOAD_URL_COLUMN] = url
    if output_path:
        row[OUTPUT_PATH_COLUMN] = output_path


def build_queue(rows: list[dict[str, str]], *, require_target_flag: bool) -> list[dict[str, str]]:
    queue: list[dict[str, str]] = []
    seen_pmids: set[str] = set()

    for row in rows:
        if require_target_flag and not is_yes(row.get(IS_FETCH_TARGET_COLUMN)):
            continue

        pmid = normalize_cell(row.get(PMID_COLUMN))
        doi = normalize_cell(row.get(DOI_COLUMN))
        if not pmid or pmid in seen_pmids:
            continue
        if existing_pdf_path(pmid) is not None:
            continue
        if not doi:
            continue

        url = resolve_best_pdf_url(doi)
        if is_blocked(url):
            continue

        row[DOWNLOAD_URL_COLUMN] = url
        queue.append(row)
        seen_pmids.add(pmid)

    return queue[BATCH_START:BATCH_START + BATCH_LIMIT]


def main() -> int:
    if not INPUT_CSV.exists():
        print(f"Input CSV not found: {INPUT_CSV}")
        return 1

    ensure_directory(WATCH_DIR)
    ensure_directory(OUTPUT_DIR)

    fieldnames, rows = load_csv_rows(INPUT_CSV)
    if not rows:
        print("Input CSV is empty.")
        return 0
    if PMID_COLUMN not in fieldnames:
        print(f"Missing required column: {PMID_COLUMN}")
        return 1

    require_target_flag = any(normalize_cell(row.get(IS_FETCH_TARGET_COLUMN)) for row in rows)
    fieldnames = ensure_columns(fieldnames, DOWNLOAD_RESULT_FIELDS + [IS_FETCH_TARGET_COLUMN, DOI_COLUMN])
    queue = build_queue(rows, require_target_flag=require_target_flag)

    if not queue:
        print("No valid browser targets in this batch.")
        return 0

    print(f"[BATCH] count={len(queue)} start={BATCH_START} limit={BATCH_LIMIT}")
    print(f"[WATCH] {WATCH_DIR}")
    print(f"[OUT]   {OUTPUT_DIR}")

    for position, row in enumerate(queue, start=1):
        pmid = normalize_cell(row.get(PMID_COLUMN))
        url = normalize_cell(row.get(DOWNLOAD_URL_COLUMN))
        destination = OUTPUT_DIR / f"{pmid}.pdf"

        print(f"\n[{position}/{len(queue)}] pmid={pmid}")
        print(f"  URL: {url}")

        if destination.exists() and destination.stat().st_size >= MIN_FILE_SIZE_BYTES:
            print(f"  [SKIP] already exists: {destination.name}")
            continue

        known = {path.resolve() for path in completed_files(WATCH_DIR)}
        started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        log_event({"event": "open", "pmid": pmid, "url": url, "started_at": started_at})

        if not DRY_RUN:
            open_url(url)
        time.sleep(OPEN_DELAY_SECONDS)

        if DRY_RUN:
            continue

        while True:
            skip_keys = "'s'" if os.name == "nt" else "'s' + Enter"
            hint = f"| press {skip_keys} to skip" if INTERACTIVE_SKIP else ""
            print(f"  [WAIT] {WAIT_TIMEOUT_SECONDS}s {hint}".rstrip())
            new_file, skipped = wait_for_download(known)

            if skipped:
                print("  [SKIP] manually skipped")
                write_result(row, status="skipped", error="manually_skipped", started_at=started_at, url=url)
                write_csv_rows(OUTPUT_CSV, fieldnames, rows)
                break

            if new_file:
                finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                print(f"  [MOVE] {new_file.name} -> {destination.name}")
                if new_file.resolve() != destination.resolve():
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
                break

            print("  [TIMEOUT] no download detected")
            if INTERACTIVE_SKIP:
                choice = input("  Retry (r) or skip (s)? ").strip().lower()
                if choice == "r":
                    continue

            write_result(row, status="failed", error="timeout", started_at=started_at, url=url)
            write_csv_rows(OUTPUT_CSV, fieldnames, rows)
            break

    print(f"Output written to: {OUTPUT_CSV}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
