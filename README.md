# Article PDF Download Assistant

This project enriches PubMed records and downloads publisher PDFs that you are
already authorized to access. The downloader opens publisher pages in Google
Chrome, applies publisher or shared-platform automation, validates completed
downloads, renames them by PMID, and records structured results in the CSV.

It does not bypass paywalls or publisher access controls. Use authorized OSU
Library access and respect publisher terms and download limits.

## Requirements

- Python 3.10 or newer
- macOS and Google Chrome for automatic clicks, tab handling, and Chrome
  cache/cookie recovery
- A Chrome profile with working OSU Library access

The Python scripts use the standard library; no third-party Python package is
currently required.

## Files and current defaults

The two scripts currently use different CSV defaults. Check or edit their path
constants before running a full metadata-to-download workflow.

| Purpose | Current path |
| --- | --- |
| Metadata input/output | `target_records.csv` |
| Downloader input/output | `target_records.csv` |
| Browser download watch directory | `~/Downloads` |
| Final PDF directory | `browser/` |
| Event log | `download_events.jsonl` |

The relevant constants are `INPUT_CSV`, `OUTPUT_CSV`, `WATCH_DIR`, and
`OUTPUT_DIR` near the top of each script. The downloader updates its configured
CSV in place after URL preparation and after every final result.

## CSV requirements

The downloader requires these lowercase columns:

- `pmid`: ASCII digits; also used as the final PDF filename
- `doi`: a DOI, DOI URL, or supported article URL

Optional selection columns:

- `is_fetch_target`: if the entire column is absent or empty, every eligible
  row is considered. Once any row has a value, only rows marked `Y` are queued.
- `skip_future_runs`: rows marked `Y` are excluded until the value is cleared
  or changed to `N`.

The downloader adds missing result columns automatically:

```text
fetch_status
fetch_source
fetch_error
fetch_error_category
fetch_error_code
fetch_error_detail
fetch_error_retryable
fetch_error_action
fetch_publisher
fetch_publisher_failure_count
download_started_at
download_finished_at
download_filename
download_url
output_path
```

`pmcid` may be present from metadata enrichment, but the downloader does not
use PMC as a PDF fallback.

## Optional metadata enrichment

Copy and edit the configuration template:

```bash
cp config.template.yaml config.yaml
```

Set:

```yaml
ncbi_email: "you@example.edu"
ncbi_api_key: ""
```

Then run:

```bash
python3 fill_pubmed_metadata.py
```

`fill_pubmed_metadata.py` reads its configured `INPUT_CSV`, fills missing
`pmcid`, `doi`, `title`, `abstract`, `journal`, and `year` values, preserves
existing non-empty values, and writes the configured output CSV atomically.

## Chrome setup

Before downloading:

1. Sign in through the OSU Library proxy in the Chrome profile configured by
   `CHROME_PROFILE_DIRECTORY`.
2. Open a subscribed publisher page and confirm institutional access.
3. Set Chrome's download directory to `~/Downloads` or change `WATCH_DIR`.
4. Turn off “Ask where to save each file before downloading.”
5. At `chrome://settings/content/pdfDocuments`, configure PDFs to download
   rather than remain only in Chrome's viewer.
6. Enable `View > Developer > Allow JavaScript from Apple Events` in Chrome.
7. Allow the terminal application to control Chrome under macOS System
   Settings > Privacy & Security > Automation.

Restart Chrome after changing Apple Events or Automation permissions.

## Run the downloader

Run all eligible records selected by the configured batch constants:

```bash
python3 download_browser_pdfs.py
```

Show command-line help:

```bash
python3 download_browser_pdfs.py --help
```

### Run one DOI

Use `--doi` to select exactly one matching CSV row:

```bash
python3 download_browser_pdfs.py --doi '10.1002/bit.27231'
```

The DOI may also be supplied as a `https://doi.org/...` value. Matching is
normalized and case-insensitive. DOI mode bypasses `BATCH_START`, `BATCH_LIMIT`,
and `is_fetch_target`, but still respects:

- `skip_future_runs=Y`
- terminal non-retryable results
- an already valid `{OUTPUT_DIR}/{pmid}.pdf`
- the requirement that the DOI exist in the configured CSV

If the command prints `No valid browser targets in this batch`, check those
conditions and confirm that the script's `INPUT_CSV` is the expected file.

## Download workflow

For each run, the downloader:

1. Validates PMIDs and DOIs, rejects duplicate PMIDs, skips persistent skips,
   and recognizes already stored PDFs.
2. Selects either the requested DOI or the `BATCH_START`/`BATCH_LIMIT` slice.
3. Applies institutional overrides, repairs recognized stale URLs, reuses safe
   saved URLs, or builds deterministic publisher routes locally.
4. Keeps unknown DOI routes as `doi.org` placeholders and resolves each one
   lazily immediately before opening it.
5. Applies blocked/manual rules and sorts the queue by download strategy while
   preserving CSV order within the same priority.
6. Opens Chrome and performs the configured publisher or platform workflow.
7. Watches `WATCH_DIR` for a new completed file. Active `.crdownload`, `.part`,
   `.download`, and `.tmp` files extend the wait while progress continues.
8. Accepts only files of at least 1,024 bytes with a PDF signature in the first
   1,024 bytes. Downloaded HTML pages are ignored.
9. Moves a valid file to `{OUTPUT_DIR}/{pmid}.pdf` and updates the CSV.

Avoid downloading unrelated PDFs while a record is waiting. ScienceDirect
downloads receive an additional filename/PII check; other publishers use the
newest qualifying PDF in the watch directory.

## Current URL repairs

The local preparation layer includes these important repairs:

- Nature `10.1038/s...` DOIs use `nature.com/articles/...`, not a Springer
  `/content/pdf/` route.
- Stale OSU-proxied MDPI and Frontiers direct-PDF URLs are converted to their
  current public PDF URLs.
- Stale Research Square and European Society of Medicine proxy URLs are
  converted to their working public article pages.
- Liebert `10.1089/...` content uses the migrated SAGE/Literatum route.
- JoVE `10.3791/...` records use JoVE's proxied `/pdf/{article_id}` application,
  which performs its authenticated PDF request; the raw
  `/api/article/pdf/{article_id}` endpoint is not used directly.
- Recognized AIP, RSC, Wiley, ACS, PNAS, ASM, Taylor & Francis, IOP, Science,
  and other DOI families receive deterministic routes.
- Research Square `10.21203/rs.3...` DOIs receive deterministic public article
  routes, while `10.1101/...` DOI pages use the shared bioRxiv/medRxiv rule.
- The Theranostics record `10.7150/thno.86921` uses its stable ProQuest document
  page; the expiring session-specific `media.proquest.com` URL is discovered
  from that page at run time.

`OVERRIDE_EXISTING_DOWNLOAD_URLS = False` still allows recognized stale URLs to
be repaired. Setting it to `True` rebuilds queued URLs from their DOI wherever
possible.

## Publisher architecture

Publisher automation is divided into reusable platform profiles and smaller
site-specific overrides:

- Silverchair: shared article-PDF selectors used by ACS, AIP, Oxford Academic,
  JAMA Network, AACR, the supplied Silverchair customer registry, and other
  compatible sites. Site-specific selectors are added when necessary.
- Literatum: shared reader/ePDF, PDF-download, and download-menu behavior used
  by Wiley, SAGE, Taylor & Francis, ASCO, NEJM, ACP, and compatible sites.
- OJS: shared galley plus viewer-download behavior used by registered OJS
  publishers, including the European Society of Medicine site.

Other publishers retain focused rules where their workflow is distinct,
including ScienceDirect, Optica, IEEE Xplore, JoVE, Springer/Nature, RSC, PLOS,
BMJ, Ovid, IOPscience, Research Square, ProQuest, bioRxiv/medRxiv, and others
registered in the script.

Recent special handling includes:

- Wiley waits up to 60 seconds for its delayed ePDF download toolbar.
- IEEE handles both legacy `stampPDF` links and Chrome's embedded PDF “Open”
  gate using a trusted keyboard action.
- JoVE waits for its authenticated PDF application to create
  `iframe#pdfIframe` before considering the browser step ready.
- RSC uses a 90-second publisher-specific click timeout.

## Skip and timeout behavior

While the script is waiting on macOS or Linux:

- Type `s` and press Enter to skip the current attempt once.
- Type `p` and press Enter to set `skip_future_runs=Y`.

`AUTO_SKIP_MODE = True` is the current default. After
`WAIT_TIMEOUT_SECONDS = 15` without a valid PDF or active temporary download,
the row is saved as `skipped` with source `auto_skip` and the batch continues.

Publisher names placed in `AUTO_SKIP_PUBLISHER_EXCEPTIONS` retain an interactive
retry/skip prompt. Names must match the label printed after `[MODE]`.

Clear `skip_future_runs` or set it to `N` to retry a persistent skip.

## Results and logging

Final PDFs currently use:

```text
browser_ooc_round1_prescreen_passed/{pmid}.pdf
```

Possible `fetch_status` values:

- `pending`: queued or prepared, without a final result yet
- `success`: a new or existing valid PDF is available
- `skipped`: blocked, persistent/manual skipped, unavailable, circuit-open, or
  auto-skipped after timeout
- `failed`: invalid input, terminal not-found, HTTP/automation failure, recovery
  exhaustion, or a non-auto-skipped timeout

Every attempt is appended as JSON Lines to `download_events.jsonl`. Failure
category, stable code, detail, retryability, recommended action, publisher, and
publisher failure count are also stored in the CSV.

CSV replacement is atomic: the script writes a temporary file and then replaces
the configured output.

## Important configuration defaults

Edit these constants near the top of `download_browser_pdfs.py` when needed:

| Setting | Current default | Purpose |
| --- | --- | --- |
| `BATCH_START` | `0` | Zero-based offset for a normal batch run. |
| `BATCH_LIMIT` | `3000` | Maximum records in a normal batch run. |
| `WAIT_TIMEOUT_SECONDS` | `15` | Initial window to discover a PDF/download. |
| `IN_PROGRESS_DOWNLOAD_TIMEOUT_SECONDS` | `900` | Maximum wait after a temporary download appears. |
| `IN_PROGRESS_DOWNLOAD_STALL_SECONDS` | `120` | Abort an unchanged temporary download. |
| `AUTO_SKIP_MODE` | `True` | Continue unattended after ordinary timeouts. |
| `AUTOMATE_PUBLISHER_PDF_CLICK` | `True` | Enable Chrome/AppleScript publisher automation. |
| `TRY_OSU_PROXY_FOR_UNCUSTOMIZED_URLS` | `True` | Proxy safe, otherwise uncustomized article pages. |
| `PUBLISHER_CLICK_TIMEOUT_SECONDS` | `20` | Default automation deadline; rules may override it. |
| `DRY_RUN` | `False` | Prepare/display without browser or CSV mutations. |
| `CLOSE_COMPLETED_AUTOMATIC_TABS` | `True` | Close a completed matching automatic tab. |

### Blocked and manual rules

`BLOCKED_URL_PATTERNS` contains case-insensitive substrings that are recorded as
configured skips. `MANUAL_URL_PATTERNS` is currently empty. A manual match stays
queued but runs after automatic strategies; a blocked match is not opened.

### Publisher circuits

Only consecutive explicit block/server signals open a publisher circuit:

- three `http_429` or `request_blocked` results
- four `http_403`, `http_502`, `http_503`, or `http_504` results

A different signal restarts the count, success clears it, and ordinary download
timeouts do not disable a publisher.

### Optica cooldown

Optica downloads run in bursts of five. A burst or detected request-limit page
starts a 300-second publisher cooldown, during which other publishers can run.
An affected Optica row can be deferred up to three times.

### ScienceDirect recovery and cleanup

ScienceDirect request-error recovery may close its tabs, stop Chrome, clear the
configured profile's cache and complete cookie databases, restart Chrome, wait,
and retry up to three times.

Current scheduled cleanup defaults:

| Setting | Default |
| --- | --- |
| `AUTO_CLEAR_BROWSER_CACHE` | `True` |
| `CACHE_CLEAR_EVERY_FILES` | `12` successful eligible-publisher PDFs |
| `CACHE_CLEAR_PUBLISHERS` | `["ScienceDirect"]` |
| `CLEAR_COOKIES_WITH_CACHE` | `True` |
| `DOWNLOAD_BREAK_EVERY_FILES` | `100` successful PDFs |
| `DOWNLOAD_BREAK_SECONDS` | `30` |

Warning: `CLEAR_COOKIES_WITH_CACHE = True` deletes the configured Chrome
profile's complete cookie databases and logs that profile out of websites. Set
it to `False` to preserve cookies, or disable scheduled cleanup with
`AUTO_CLEAR_BROWSER_CACHE = False`.

## Dry run and tests

For a queue preview without opening the browser, moving PDFs, or writing CSV
changes, temporarily set:

```python
DRY_RUN = True
```

The configured watch/output directories may still be created, and validation or
skip events may still be appended to `BATCH_LOG_PATH`.

Run the regression suite with:

```bash
python3 -m unittest -v
```

Check syntax with:

```bash
python3 -m py_compile download_browser_pdfs.py test_download_browser_pdfs.py
```

## Troubleshooting

### Chrome blocks automatic clicks

Confirm both Chrome's `Allow JavaScript from Apple Events` option and the
terminal application's macOS Automation permission, then restart Chrome.

### A publisher page opens but nothing downloads

- Complete any OSU, Duo, publisher login, or consent prompt.
- Confirm Chrome downloads into `WATCH_DIR` without asking for a location.
- Confirm PDF handling is set to download files.
- Review the `[AUTO-PDF]` detail and the matching event in
  `download_events.jsonl`.
- Run only that record with `--doi` while diagnosing its page.

### An HTML file appears in Downloads

The validator ignores HTML because it lacks a PDF signature. An HTML file
usually indicates a publisher viewer, access/error page, or outdated selector.
Record the DOI, terminal output, final browser URL, and the relevant download
button/iframe HTML for inspection.

### The wrong PDF is moved

Do not download unrelated PDFs while the script waits. Remove the incorrect
output, place the verified PDF at `{OUTPUT_DIR}/{pmid}.pdf`, or rerun the DOI
after clearing the conflicting file.

### Chrome uses a different profile

Open `chrome://version`, inspect **Profile Path**, and set
`CHROME_PROFILE_DIRECTORY` to the final directory name such as `Profile 1`.

### Other operating systems or browsers

PDF validation, renaming, and CSV updates are portable, but the verified click,
tab, and Chrome cleanup workflows are macOS-specific. For a manual workflow,
disable:

```python
AUTOMATE_PUBLISHER_PDF_CLICK = False
AUTO_CLEAR_BROWSER_CACHE = False
CLOSE_COMPLETED_AUTOMATIC_TABS = False
CLOSE_SCIENCEDIRECT_TABS_AT_BREAK = False
```
