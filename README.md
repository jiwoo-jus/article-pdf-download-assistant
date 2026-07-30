# Article PDF Download Assistant

This tool helps turn a CSV of PubMed IDs into:

1. A metadata-enriched CSV
2. A folder of downloaded article PDFs

It opens direct PDF links where a publisher provides a predictable link, detects
completed downloads, renames PDFs by PMID, and updates the CSV. Verified browser
patterns are attempted first; records that need a login, an institutional
viewer, or an unknown publisher interaction are placed at the end for manual
review.

This tool does not bypass paywalls. It only works for articles you can already
access in your browser. Use normal, authorized OSU Library access and respect
publisher download limits and terms.

You will need Google Chrome on macOS for automatic publisher clicks, tab management, and cache/cookie cleanup.

---

## 1. Install Requirements

- Python 3.10 or newer
- The metadata script requires `requests`:

```bash
python -m pip install requests
```

---

## 2. Prepare `config.yaml`

`ncbi_email` is required for PubMed metadata retrieval.

`ncbi_api_key` is optional but recommended.

Replace the example values in your information.

---

## 3. Prepare `target_records.csv`

The minimum input is a CSV with a `pmid` column:

```csv
pmid
37313651
18523888
23805007
```

Replace the example PMIDs with your target PMIDs.

After running the scripts, the CSV will be updated with metadata and download
information, including:

```text
pmcid
doi
title
abstract
journal
year
fetch_status
fetch_source
fetch_error
download_started_at
download_finished_at
download_filename
download_url
output_path
is_fetch_target
```

---

## 4. Fill PubMed Metadata

Run:

```bash
python fill_pubmed_metadata.py
```

This script:

* Reads `target_records.csv`
* Retrieves PubMed metadata using NCBI E-utilities
* Fills missing metadata only
* Keeps existing non-empty values unchanged
* Saves the updated CSV back to `target_records.csv`

At the end, it prints how many rows were targeted, updated, or unresolved.

---

## 5. Choose Download Targets (optional)

By default, the downloader will try to download PDFs for all rows with a DOI.

To download only selected rows, add or edit the `is_fetch_target` column and mark
target rows with `Y`:

```csv
pmid,doi,is_fetch_target
37313651,10.xxxx/example,Y
18523888,10.xxxx/example,
23805007,10.xxxx/example,Y
```

Important:

* If `is_fetch_target` is missing or completely empty, all rows with a DOI are treated as targets.
* If any row has a value in `is_fetch_target`, only rows with exactly `Y` will be downloaded.

---

## 6. Configure Your Browser

### Sign in through OSU Library

Before running the
downloader:

1. Sign in through the OSU Library proxy and complete Duo or other required
   authentication.
2. Open one subscribed publisher link and verify that the page recognizes your
   institutional access.
3. Keep the same Chrome profile signed in while the downloader runs.

If a cookie-consent alert appears, choose **Necessary only**, **Reject optional**,
or the publisher's equivalent, and save the preference. Essential cookies still
need to remain enabled for OSU and publisher login sessions. If authentication
loops, allow cookies for the OSU proxy and that publisher, then sign in again.

### Configure downloads

The browser must download files to your normal Downloads folder:

```text
Windows: C:\Users\{your_username}\Downloads
macOS:   /Users/{your_username}/Downloads
```

For example:

```text
/Users/park.3620/Downloads
```

Check the download location in Chrome at:

```text
chrome://settings/downloads
```

Or in Edge at:

```text
edge://settings/downloads
```

Make sure:

* Download location is set to your Downloads folder
* “Ask where to save each file before downloading” is turned off

Also configure PDFs to download instead of opening in the browser viewer:

```text
Chrome: chrome://settings/content/pdfDocuments
Edge:   edge://settings/content/pdfDocuments
```

Enable “Download PDFs” in Chrome or “Always download PDF files” in Edge. This
setting is important: without it, a direct PDF link may open in the viewer and
still require a manual click on the viewer's download icon.

### Enable Chrome automation on macOS

Automatic publisher-button clicks use Google Chrome and AppleScript on macOS.
In Chrome, enable:

```text
View > Developer > Allow JavaScript from Apple Events
```

Allow the terminal application running Python to control Chrome under:

```text
System Settings > Privacy & Security > Automation
```

Restart Chrome after changing either setting. If the terminal reports that
JavaScript is blocked, confirm the Chrome menu option again.

Verified automatic click rules currently require Chrome on macOS. On another
operating system, or when using Safari, Edge, or Firefox, publisher pages still
open but their PDF buttons may require manual clicks. To force all records into
the default-browser/manual workflow, set:

```python
AUTOMATE_PUBLISHER_PDF_CLICK = False
```

---

## 7. Download PDFs

Run:

```bash
python download_browser_pdfs.py
```

For each queued row, the script:

1. Reuses a valid existing `browser/{pmid}.pdf`, if present.
2. Chooses an institutional override, refreshes or reuses `download_url`
   according to `OVERRIDE_EXISTING_DOWNLOAD_URLS`, then uses a known DOI route
   or resolves the DOI through `doi.org`.
3. Applies configured blocked/manual URL rules.
4. Sorts verified automatic routes ahead of manual-review routes.
5. Opens the URL and attempts a supported publisher click sequence when
   applicable.
6. Watches `~/Downloads` for a new or changed file.
7. Accepts only a file of at least 1,024 bytes containing a PDF signature in
   its first 1,024 bytes.
8. Moves the file to `browser/{pmid}.pdf`.
9. Updates `target_records.csv` after each final result.

Avoid downloading unrelated PDFs while the script is waiting: it detects the
newest qualifying PDF and cannot independently prove that it belongs to the
current article.

### Skip or retry

While waiting on macOS or Linux, enter `s` and press `Enter` to skip the current
record. On Windows, press `s`.

The default wait is 600 seconds. After a timeout, an interactive run prompts you
to retry with `r`; any other response records a timeout failure.

## URL preparation and ordering

URL selection uses this order:

1. `INSTITUTIONAL_URL_OVERRIDES`
2. If `OVERRIDE_EXISTING_DOWNLOAD_URLS` is `True`, rebuild the URL from `doi`
3. Otherwise, reuse an HTTP/HTTPS URL already stored in `download_url`
4. A known DOI-prefix route
5. A `HEAD` request to `doi.org`, followed by publisher-specific URL rewriting
6. The original `doi.org` URL if resolution fails

URL preparation reports its own progress before browser downloads begin:

```text
[URL PREP] selected=1431 eligible=1431 start=0 limit=2000 override_existing=True
[URL 1/1431] pmid 42415230 | refresh existing URL
...
[URL PREP DONE] queued=1431 blocked=0 | refreshed=1200 reused=200 ...
```

The URL messages mean:

- `RESOLVE HEAD` or `RESOLVE GET`: the DOI resolver successfully redirected to
  a publisher. `GET` is tried when the lighter `HEAD` request fails.
- `REWRITE`: a resolved article page was converted to the publisher's preferred
  PDF or full-article route. This is not an error.
- `REFRESH URL`: with `OVERRIDE_EXISTING_DOWNLOAD_URLS = True`, a newly prepared
  URL replaced the saved value.
- `KEEP URL`: refresh failed and returned only a generic DOI fallback, so the
  existing publisher URL was preserved.
- `RESOLVE FALLBACK`: both lookup methods failed. If no better existing URL is
  available, the browser receives the `doi.org` URL and follows it interactively.

Common resolver failures are `HTTP 403` (the publisher refused an automated
lookup), `HTTP 302` (a redirect loop or rejected redirect), and timeout/network
errors. They do not necessarily mean the article is unavailable. To avoid
repeating resolver requests after URLs have been prepared, set:

```python
OVERRIDE_EXISTING_DOWNLOAD_URLS = False
```

The default blocked rule is:

```python
BLOCKED_URL_PATTERNS = ["karger.com"]
```

Matching is a case-insensitive substring search. A matching row is not opened
and is recorded as:

```text
fetch_status: skipped
fetch_source: skip_rule
fetch_error: blocked_url_pattern:karger.com
```

The default manual-review rule is:

```python
MANUAL_URL_PATTERNS = ["link.springer.com"]
```

A manual-rule match remains queued but is processed after automatic routes.

Known automatic click rules currently cover:

- ScienceDirect
- ACS
- Wiley
- SAGE
- RSC
- Taylor & Francis
- Oxford Academic
- IOPscience
- IIAR Journals
- AACR Journals
- JAMA Network

Recognizable direct-PDF URLs are also placed in the automatic tier. EBSCO
institutional viewer URLs and unknown publisher layouts remain in the
manual-review tier. Stable CSV ordering is preserved within the same priority.

## Output

PDFs are stored as:

```text
browser/{pmid}.pdf
```

The script adds missing result columns to `target_records.csv`:

```text
fetch_status
fetch_source
fetch_error
download_started_at
download_finished_at
download_filename
download_url
output_path
```

Possible statuses are:

- `pending`: queued, but no final result has been written yet
- `success`: a new or existing valid PDF is available
- `skipped`: a duplicate, blocked URL, or manual skip
- `failed`: validation, timeout, or ScienceDirect recovery failure

CSV updates are written through a temporary file and atomically replace the
original CSV.

## Configuration

Configuration is currently done by editing constants near the top of
`download_browser_pdfs.py`.

### Paths and files

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROJECT_ROOT` | Directory containing the script | Base directory used to derive project-relative paths. |
| `INPUT_CSV` | `PROJECT_ROOT / "target_records.csv"` | CSV read at startup. |
| `OUTPUT_CSV` | `INPUT_CSV` | CSV replaced with updated rows. Set a different path to preserve the input file. |
| `WATCH_DIR` | `Path.home() / "Downloads"` | Directory polled for newly completed browser downloads. |
| `OUTPUT_DIR` | `PROJECT_ROOT / "browser"` | Destination for PDFs renamed as `{pmid}.pdf`. |
| `BATCH_LOG_PATH` | `None` | Optional JSON Lines event log. Set to a `Path`; `None` disables event logging. |

All four path constants are resolved to absolute paths by the script.

### CSV schema variables

These constants define column names. Change them only if the input/output CSV
uses a different schema.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PMID_COLUMN` | `"pmid"` | Column containing the PubMed ID and source of the output filename. |
| `DOI_COLUMN` | `"doi"` | Column containing a DOI, DOI URL, or article URL. |
| `IS_FETCH_TARGET_COLUMN` | `"is_fetch_target"` | Optional row-selection column. |
| `TARGET_ENABLED_VALUE` | `"Y"` | Selection marker, matched case-insensitively after trimming whitespace. |
| `FETCH_STATUS_COLUMN` | `"fetch_status"` | Final or pending state of the row. |
| `FETCH_SOURCE_COLUMN` | `"fetch_source"` | Component that produced the result. |
| `FETCH_ERROR_COLUMN` | `"fetch_error"` | Machine-readable failure or skip reason. |
| `DOWNLOAD_STARTED_AT_COLUMN` | `"download_started_at"` | Local ISO-8601 timestamp recorded before opening the URL. |
| `DOWNLOAD_FINISHED_AT_COLUMN` | `"download_finished_at"` | Local ISO-8601 timestamp recorded when the attempt finishes. |
| `DOWNLOAD_FILENAME_COLUMN` | `"download_filename"` | Final basename, normally `{pmid}.pdf`. |
| `DOWNLOAD_URL_COLUMN` | `"download_url"` | Prepared URL used for the attempt. Existing values are refreshed or reused according to `OVERRIDE_EXISTING_DOWNLOAD_URLS`. |
| `OUTPUT_PATH_COLUMN` | `"output_path"` | Absolute path of a successfully stored PDF. |
| `DOWNLOAD_RESULT_FIELDS` | The eight result columns above | Internal ordered list of result columns added when missing. |

### Batch and download controls

| Variable | Default | Meaning and valid values |
| --- | --- | --- |
| `BATCH_START` | `0` | Zero-based offset into eligible records. Must be `>= 0`. |
| `BATCH_LIMIT` | `2000` | Maximum eligible records selected. Must be `> 0`. |
| `OVERRIDE_EXISTING_DOWNLOAD_URLS` | `True` | `True` rebuilds each queued row's URL from `doi` and stores it in `download_url`; `False` reuses an existing HTTP/HTTPS `download_url` and only resolves rows without one. Institutional overrides always take precedence. |
| `OPEN_DELAY_SECONDS` | `0.4` | Pause after initiating a URL open. Non-negative seconds are expected. |
| `WAIT_TIMEOUT_SECONDS` | `600` | Maximum seconds to wait for a PDF before prompting/failing. Must be `> 0`. |
| `POLL_INTERVAL_SECONDS` | `0.3` | Delay between scans of `WATCH_DIR`. Must be `> 0`. |
| `MIN_FILE_SIZE_BYTES` | `1024` | Minimum accepted PDF size in bytes. |
| `INTERACTIVE_SKIP` | `True` | `True` enables keyboard skipping and the retry prompt; `False` turns a timeout directly into failure. |
| `DRY_RUN` | `False` | `True` prepares/displays the queue without opening URLs, moving PDFs, or writing CSV changes. |
| `OPEN_COMMAND` | `None` | Optional command string used instead of Chrome automation/default-browser opening, such as `"open -a Safari"`. `None` uses normal behavior. |
| `BLOCKED_URL_PATTERNS` | `["karger.com"]` | Case-insensitive URL substrings that cause a row to be recorded as skipped. Use `[]` to disable. |
| `MANUAL_URL_PATTERNS` | `["link.springer.com"]` | Case-insensitive URL substrings forced into the manual-review tier. Use `[]` to disable. |
| `AUTOMATE_PUBLISHER_PDF_CLICK` | `True` | Enables supported Chrome/AppleScript publisher clicks. |
| `PUBLISHER_CLICK_TIMEOUT_SECONDS` | `20` | Maximum publisher automation window in seconds. |
| `CLOSE_COMPLETED_AUTOMATIC_TABS` | `True` | Closes the active automated tab after its PDF is detected, if the tab still matches the expected host. |

### Cleanup, breaks, and recovery

| Variable | Default | Meaning and valid values |
| --- | --- | --- |
| `AUTO_CLEAR_BROWSER_CACHE` | `True` | Enables scheduled Chrome cleanup after successful downloads. |
| `CACHE_CLEAR_EVERY_FILES` | `12` | Eligible-publisher successful-download interval for cleanup. Must be `> 0` while automatic cleanup is enabled. |
| `CACHE_CLEAR_PUBLISHERS` | `["ScienceDirect"]` | Publisher labels whose successful new downloads count toward and can trigger scheduled cleanup. Other publishers do not trigger cache or cookie cleanup. |
| `CLEAR_COOKIES_WITH_CACHE` | `True` | `True` also closes Chrome and deletes the configured profile's complete cookie databases; `False` preserves cookies. |
| `DOWNLOAD_BREAK_EVERY_FILES` | `1000` | Successful-download interval for the scheduled break. Must be `> 0`. |
| `DOWNLOAD_BREAK_SECONDS` | `60` | Break duration and ScienceDirect recovery delay. Must be `>= 0`. |
| `SCIENCEDIRECT_REQUEST_ERROR_MAX_RETRIES` | `3` | Maximum cleanup/retry cycles for one detected ScienceDirect request error. Must be `>= 0`; `0` disables recovery retries. |
| `CLOSE_SCIENCEDIRECT_TABS_AT_BREAK` | `True` | Closes ScienceDirect tabs whenever the download-break interval is reached. |
| `CHROME_PROFILE_DIRECTORY` | `"Default"` | Final directory name of the Chrome profile targeted by cleanup, for example `"Profile 1"`. |

### Derived Chrome paths

These values are built from `CHROME_PROFILE_DIRECTORY` for macOS. Normally,
change the profile variable rather than editing these derived paths.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CHROME_CACHE_ROOT` | `~/Library/Caches/Google/Chrome` | Root of Chrome's macOS cache data. |
| `CHROME_USER_DATA_ROOT` | `~/Library/Application Support/Google/Chrome` | Root of Chrome's macOS profile data. |
| `CHROME_CACHE_DIRECTORIES` | `<profile>/Cache`, `<profile>/Code Cache` | Directory contents removed by cache cleanup. |
| `CHROME_PROFILE_ROOT` | `CHROME_USER_DATA_ROOT / CHROME_PROFILE_DIRECTORY` | Selected Chrome profile directory. |
| `CHROME_COOKIE_DATABASES` | `<profile>/Cookies`, `<profile>/Network/Cookies` | Complete cookie databases targeted when cookie cleanup is enabled. |
| `CHROME_COOKIE_FILES` | Each cookie database plus `-journal`, `-wal`, and `-shm` | Exact database/companion files deleted during cookie cleanup. |

### Routing and priority variables

Lower numeric priorities run first. Sorting is stable, so CSV order is
preserved among rows with the same priority.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AUTO_PUBLISHER_PRIORITY` | ScienceDirect `0`; ACS `10`; Wiley `20`; SAGE `30`; RSC `40`; Taylor & Francis `50`; Oxford Academic `60`; IOPscience `70`; IIAR Journals `80`; AACR Journals `90`; JAMA Network `100` | Processing order for verified publisher click rules. |
| `DIRECT_PDF_PRIORITY` | `200` | Priority for recognizable direct-PDF URLs. |
| `MANUAL_REVIEW_PRIORITY` | `1000` | Priority for configured manual patterns, EBSCO viewers, and unknown layouts. |
| `INSTITUTIONAL_URL_OVERRIDES` | One DOI-to-OSU-EBSCO mapping | Exact DOI-to-URL mappings for opaque institutional viewer URLs. Use `{}` when no overrides are needed. |
| `TEMP_SUFFIXES` | `{".crdownload", ".part", ".download", ".tmp"}` | Files and companion files treated as incomplete downloads. |
| `DOI_PREFIX_RULES` | Rules for `10.3390`, `10.1007`, `10.1038`, `10.1002`, `10.1111`, `10.1096`, `10.1021`, `10.3389`, `10.1073`, `10.1126`, `10.1128`, `10.1177`, `10.1089`, `10.1080`, and `10.1088` | Ordered DOI-prefix-to-publisher URL templates checked before resolving through `doi.org`. |

### Publisher endpoint variables

These are internal URL-building defaults, not ordinary runtime switches.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCIENCEDIRECT_PROXY_HOST` | `www-sciencedirect-com.proxy.lib.ohio-state.edu` | OSU ScienceDirect proxy host recognized by routing and click rules. |
| `SCIENCEDIRECT_ARTICLE_BASE_URL` | ScienceDirect proxy `/science/article/pii` URL | Base URL used when rewriting Elsevier PII links. |
| `ACS_PROXY_HOST` | `pubs-acs-org.proxy.lib.ohio-state.edu` | OSU ACS proxy host. |
| `ACS_ARTICLE_BASE_URL` | ACS proxy `/doi/full` URL | Base URL used for ACS DOI routes. |
| `WILEY_PROXY_HOST` | `onlinelibrary-wiley-com.proxy.lib.ohio-state.edu` | OSU Wiley proxy host. |
| `WILEY_ARTICLE_BASE_URL` | Wiley proxy `/doi/full` URL | Base URL used for Wiley DOI routes. |
| `SAGE_PROXY_HOST` | `journals-sagepub-com.proxy.lib.ohio-state.edu` | OSU SAGE proxy host. |
| `SAGE_ARTICLE_BASE_URL` | SAGE proxy `/doi/full` URL | Base URL used for SAGE DOI routes. |
| `LIEBERT_ARTICLE_BASE_URL` | `https://www.liebertpub.com/doi/pdf` | Public Liebert PDF base URL. |
| `TANDF_PROXY_HOST` | `www-tandfonline-com.proxy.lib.ohio-state.edu` | OSU Taylor & Francis proxy host. |
| `TANDF_ARTICLE_BASE_URL` | Taylor & Francis proxy `/doi/full` URL | Base URL used for Taylor & Francis DOI routes. |

## Enum and state reference

### `PublisherOpenStatus`

This is the only Python `Enum` defined by the script.

| Enum member | String value | Meaning |
| --- | --- | --- |
| `PublisherOpenStatus.OPENED` | `"opened"` | The URL opened or an automatic PDF click was initiated successfully. |
| `PublisherOpenStatus.REQUEST_BLOCKED` | `"request_blocked"` | The configured ScienceDirect request-error page was detected; recovery may run. |
| `PublisherOpenStatus.AUTOMATION_FAILED` | `"automation_failed"` | Chrome automation was unavailable or failed; `open_url()` falls back to the default browser. |

### CSV state values

These are strings written to the CSV, not Python Enums.

| Field | Available values | Meaning |
| --- | --- | --- |
| `fetch_status` | `pending`, `success`, `skipped`, `failed` | Lifecycle/result state described in the Output section. |
| `fetch_source` | `browser`, `existing`, `validation`, `skip_rule` | Browser attempt, pre-existing PDF, input validation, or blocked-pattern rule. |
| `fetch_error` | Empty, `invalid_pmid`, `invalid_doi`, `duplicate_pmid`, `manually_skipped`, `timeout`, `blocked_url_pattern:<pattern>`, `sciencedirect_request_error_retries_exhausted`, `sciencedirect_request_error_cleanup_failed` | Machine-readable reason; empty means no error was recorded. |

### Internal record fields

| Type | Field | Meaning |
| --- | --- | --- |
| `PublisherClickRule` | `publisher` | Human-readable publisher label and priority-map key. |
| `PublisherClickRule` | `pdf_selector` | CSS selector used for the first PDF/viewer click. |
| `PublisherClickRule` | `download_selector` | Optional CSS selector for a second-stage download link. |
| `PublisherClickRule` | `reveal_download_selector` | Optional selector that opens a download menu. |
| `PublisherClickRule` | `dismiss_selector` | Optional popup-dismiss selector. |
| `PublisherClickRule` | `request_error_selector` | Optional selector for a publisher error message. |
| `PublisherClickRule` | `request_error_text` | Text required within the selected error element. |
| `FileSnapshot` | `size` | Observed file size in bytes. |
| `FileSnapshot` | `modified_ns` | File modification timestamp in nanoseconds. |
| `DownloadStrategy` | `priority` | Numeric queue-order priority. |
| `DownloadStrategy` | `automatic` | Whether the route is treated as automatic. |
| `DownloadStrategy` | `label` | Publisher or manual-route description printed to the terminal. |

`BATCH_START` and `BATCH_LIMIT` select from eligible records after validation
and existing-file checks. The selected records are then reordered by download
strategy.

Set `BATCH_LOG_PATH` to a `Path` to append JSON Lines event records. Set
`OPEN_COMMAND` to a command string to use a custom URL opener; doing so bypasses
the Chrome publisher-click automation.

Set `DRY_RUN = True` to prepare and display the queue without opening URLs,
moving downloads, or writing CSV results. The script can still resolve DOI URLs
over the network and create the configured watch/output directories.

## Scheduled breaks and Chrome cleanup

After every `CACHE_CLEAR_EVERY_FILES` successful new ScienceDirect downloads by default, the script:

1. Clears Chrome cache and cookies, if enabled.
2. Continues with the next record.

Successful downloads from publishers not listed in `CACHE_CLEAR_PUBLISHERS` do
not count toward this interval and never trigger scheduled cache/cookie cleanup.
The separate `DOWNLOAD_BREAK_EVERY_FILES`-file download-break counter still includes successful new PDFs
from every publisher.

Cache cleanup targets the configured Chrome profile's `Cache` and `Code Cache`
directories.

Important: with `CLEAR_COOKIES_WITH_CACHE = True`, Chrome is closed and the
script deletes the configured profile's complete `Cookies` and
`Network/Cookies` database files, including journal/WAL/SHM companions. This
logs the profile out of websites; it does not preserve other
sessions selectively. Chrome is reopened if it was running.

To preserve all cookies during scheduled cleanup:

```python
CLEAR_COOKIES_WITH_CACHE = False
```

ScienceDirect may still require regular cookie cleanup when it reports too many
requests.

To disable scheduled cache cleanup entirely:

```python
AUTO_CLEAR_BROWSER_CACHE = False
```

## ScienceDirect request-error recovery

The ScienceDirect rule detects an error page containing:

```text
There was a problem providing the content you requested
```

When detected, the script:

1. Closes ScienceDirect tabs.
2. Stops Chrome.
3. Clears the configured profile's cache and complete cookie database.
4. Restarts Chrome.
5. Waits `DOWNLOAD_BREAK_SECONDS`.
6. Retries the same article.

Recovery is attempted at most
`SCIENCEDIRECT_REQUEST_ERROR_MAX_RETRIES` times. The row is marked failed if
retries are exhausted or cleanup/restart does not fully complete.

Because recovery clears all cookies, be prepared to sign in through OSU again.
Local cleanup also cannot remove a server-side publisher or IP restriction. Do
not use other devices, networks, or addresses to evade publisher limits.

## Other operating systems and browsers

Download detection, PDF validation, renaming, and CSV updates can work with any
browser that downloads into `WATCH_DIR`. Automatic publisher clicks, tab
closing, and the configured Chrome cleanup paths are macOS-specific.

For another operating system or browser, use:

```python
AUTOMATE_PUBLISHER_PDF_CLICK = False
AUTO_CLEAR_BROWSER_CACHE = False
CLOSE_COMPLETED_AUTOMATIC_TABS = False
CLOSE_SCIENCEDIRECT_TABS_AT_BREAK = False
```

The scheduled download break works independently of browser automation.

## Troubleshooting

### A page opens but nothing downloads

- Complete any institutional or publisher login prompt.
- Dismiss a consent dialog that covers the PDF control.
- Click the PDF or Download control manually.
- Confirm that the browser downloads into `WATCH_DIR`.
- Confirm that PDFs download instead of opening in the browser viewer.
- On macOS, verify Chrome's Apple Events setting and Automation permission.

### The automatic click is blocked

Enable:

```text
View > Developer > Allow JavaScript from Apple Events
```

Then check the terminal application's macOS Automation permission, restart
Chrome, and retry.

### The wrong PDF was moved

Do not download other PDFs while a record is waiting. If necessary, move the
incorrect file out of `browser/`, download the correct PDF manually as
`browser/{pmid}.pdf`, and rerun the script.

### Chrome uses a different profile

Open `chrome://version` and inspect **Profile Path**. Set
`CHROME_PROFILE_DIRECTORY` to its final directory name, such as `"Profile 1"`.

### Manual retrieval

For unavailable records, retrieve the article through an authorized source such
as OSU Library, the publisher, PubMed Central, or interlibrary loan. Save the
verified file as `browser/{pmid}.pdf`; the next run will recognize it as an
existing result.
