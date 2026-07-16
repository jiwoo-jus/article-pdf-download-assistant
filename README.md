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

---

## 1. Install Requirements

Python 3.9 or newer is recommended. The browser downloader uses only the Python
standard library. The metadata script requires `requests`:

```bash
python -m pip install requests
```

---

## 2. Prepare `config.yaml`

`ncbi_email` is required for PubMed metadata retrieval.

`ncbi_api_key` is optional but recommended.

Create `config.yaml` from `config.template.yaml` and fill in your information.

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

The first automation attempt may also trigger a macOS permission prompt. Allow
the terminal application running Python to control Google Chrome. This can be
reviewed later under:

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
python download_browser_pdfs_original2.py
```

The selected batch is reordered into two tiers.

### Tier 1: verified automatic patterns

The script attempts these first:

- ScienceDirect. ACS, Wiley, and other tested publisher click rules: SAGE, RSC, 
Taylor & Francis, Oxford Academic, IOPscience, IIAR Journals, AACR Journals, JAMA Network
- Recognizable direct PDF URLs

### Tier 2: possible manual review

The remaining records open after the automatic tier. These include EBSCO institutional viewers, 
unknown publisher layouts, access prompts, and pages whose PDF controls could not be identified safely. 
Complete the OSU login or click the PDF/download controls while the script waits.

For each target row, the script then:

1. Read the PMID and DOI from `target_records.csv`
2. Select an institutional override, existing `download_url`, predictable DOI
   route, or resolved DOI URL
3. Process verified automatic patterns before manual-review records
4. Wait for a PDF download
5. Let you manually complete login or click PDF/Download when needed
6. Verify that the downloaded content has a PDF signature
7. Move and rename it as:

```text
browser/{pmid}.pdf
```

For example:

```text
browser/37313651.pdf
```

8. Update `target_records.csv` with the download status and output path

During each record, the terminal will show:

```text
[WAIT] 600s | press 's' + Enter to skip
```

On Windows, press `s` to skip the current record. On macOS and Linux, press
`s` and Enter.

If no file is detected before the timeout, the script will let you retry or skip.

---

## 8. Output

Downloaded PDFs are saved in:

```text
browser/
```

Example:

```text
browser/37313651.pdf
browser/18523888.pdf
```

Each row in `target_records.csv` will receive one of these statuses:

```text
pending
success
skipped
failed
```

`pending` means that a row is queued or the process stopped before its final
result was recorded. `fetch_source` distinguishes browser attempts, existing
files, validation failures, and configured skip rules.

The script automatically skips rows that already have a valid PDF at:

```text
browser/{pmid}.pdf
```

---

## 9. Batch Settings (optional)

You can adjust these constants near the top of
`download_browser_pdfs_original2.py`:

```python
BATCH_START = 0
BATCH_LIMIT = 1000
WAIT_TIMEOUT_SECONDS = 600
OPEN_DELAY_SECONDS = 0.5
AUTOMATE_PUBLISHER_PDF_CLICK = True
AUTO_CLEAR_BROWSER_CACHE = True
CACHE_CLEAR_EVERY_FILES = 10
CLEAR_COOKIES_WITH_CACHE = False
DOWNLOAD_BREAK_EVERY_FILES = 10
DOWNLOAD_BREAK_SECONDS = 60
CLOSE_COMPLETED_AUTOMATIC_TABS = True
CLOSE_SCIENCEDIRECT_TABS_AT_BREAK = True
CHROME_PROFILE_DIRECTORY = "Default"

BLOCKED_URL_PATTERNS = ["karger.com"]
MANUAL_URL_PATTERNS = ["link.springer.com"]
```

With automatic cleanup enabled, the downloader removes Chrome's cached web
files after every configured number of newly downloaded PDFs. Cookie deletion
is disabled by default, so Chrome stays open and browser login sessions are
preserved. If `CLEAR_COOKIES_WITH_CACHE` is changed to `True`, cleanup briefly
quits Chrome, removes the cookie database, and reopens Chrome. Browsing history
and saved passwords are not removed. If Chrome uses a different profile, set
`CHROME_PROFILE_DIRECTORY` to its directory name, such as `"Profile 1"`.

Completed automatic article tabs are closed after their PDF is detected. After
every 10 successful new downloads, any remaining ScienceDirect tabs are closed,
cache cleanup runs, and the downloader waits 60 seconds before opening the next
record. Tabs for unrelated websites are not closed.

`BLOCKED_URL_PATTERNS` uses case-insensitive substring matching against the final
download URL. Add or remove domains or URL fragments freely. Set it to `[]` to
disable this feature:

```python
BLOCKED_URL_PATTERNS = []
```

A matching row is not opened in the browser. Its CSV result records:

```text
fetch_status: skipped
fetch_source: skip_rule
fetch_error: blocked_url_pattern:karger.com
```

The matched URL remains available in `download_url`.

`MANUAL_URL_PATTERNS` uses the same case-insensitive substring matching, but a
matching row is not skipped. It is moved into the manual-review tier, opened
after the automatic tier, and left for you to complete any login or PDF click.
Set it to `[]` if every recognized URL should use its normal automatic strategy:

```python
MANUAL_URL_PATTERNS = []
```

Some OSU institutional viewer URLs cannot be derived from a DOI because they
contain an opaque database record ID. Configure those explicitly:

```python
INSTITUTIONAL_URL_OVERRIDES = {
    "10.xxxx/example": "https://institutional-viewer.example/path",
}
```

The selected source batch is sorted into automatic and manual-review tiers after
URL preparation. Ordering inside the same publisher group remains stable.

Examples:

```python
# First 25 records
BATCH_START = 0
BATCH_LIMIT = 25

# Next 25 records
BATCH_START = 25
BATCH_LIMIT = 25

# Longer timeout
WAIT_TIMEOUT_SECONDS = 900
```

---

## 10. Troubleshooting

### Browser opens a page but nothing downloads

Try the following:

* Complete any OSU or publisher login prompt.
* Accept necessary cookies and reject optional cookies if a consent alert blocks
  the page.
* Click the PDF or Download button manually.
* Confirm Chrome downloads to the watched Downloads folder.
* Confirm Chrome is configured to download PDFs instead of displaying them.
* On macOS, confirm **Allow JavaScript from Apple Events** and the Automation
  permission are enabled.
* Press `r` to retry after a timeout, or `s` and Enter to skip.

### Chrome says the automatic click is blocked

Confirm this Chrome option is checked:

```text
View > Developer > Allow JavaScript from Apple Events
```

Then check macOS **System Settings > Privacy & Security > Automation** and allow
the terminal application to control Chrome. Restart Chrome and retry one record.

### Login or cookie prompt repeats

Use a normal Chrome window, not Incognito. Save the publisher's necessary-cookie
preference, allow essential cookies for the OSU proxy and publisher, and sign in
again. Optional advertising and analytics cookies are not required by this tool.

### Wrong file was moved

The downloader validates PDF content, but it still cannot know whether an
unrelated PDF belongs to the active article. Avoid downloading other PDFs while
the script is waiting.

### If ScienceDirect reports “There was a problem providing the content”

An error page containing a reference number, IP address, user agent, and
timestamp usually means ScienceDirect has temporarily blocked that IP after
detecting unusual activity. [Elsevier says these blocks are automatically
released and that programmatic ScienceDirect website access is not
permitted](https://service.elsevier.com/app/answers/detail/a_id/10117/supporthub/sciencedirect/).

1. Stop processing ScienceDirect records and avoid repeated retries.
2. Save a screenshot plus the reference number, IP address, and timestamp.
3. Wait and try a single article manually later.

If the problem appears to be a stale browser session rather than an IP block,
clear only ScienceDirect and OSU-proxy site data through Chrome's privacy/site
settings, restart Chrome, and sign in through OSU again. Chrome's built-in site
data controls are preferred. If using a cache-cleaning extension, use a trusted
extension with the narrowest permissions possible.

Changing browsers can help diagnose a browser-local cache or profile problem,
but it does not remove a server-side IP block. Do not change devices, networks,
or IP addresses to bypass a publisher restriction.

### If ScienceDirect reports a daily download maximum

Stop ScienceDirect downloads for the day and follow the limit displayed by the
site. Elsevier currently documents multiple-download limits of 100 documents per
day from search results and 250 per day from a journal issue page in its
[download guidance](https://service.elsevier.com/app/answers/detail/a_id/10538/supporthub/sciencedirect/).
Limits and individual-download rules can change. Wait for the permitted reset
window or contact OSU Library/Elsevier Support if access is needed for a
legitimate larger research workflow. Do not attempt to evade the limit by
switching IP addresses, networks, or laptops.

---

## 11. Authorized Manual Retrieval

For skipped or unavailable records, use the DOI with one of these authorized
routes:

* OSU Library catalog, journal search, or **Find It at OSU**
* The publisher page while signed in through OSU
* PubMed Central or another legitimate open-access repository
* OSU interlibrary loan/document delivery

After downloading manually, place the verified PDF at `browser/{pmid}.pdf`. The
next run will recognize it as an existing result.

---

## 12. Most features are available only on macOS + Chrome.

- Automatic publisher clicks: Google Chrome on macOS only.
- Automatic tab closing: Google Chrome on macOS only.
- Cache/cookie cleanup: Google Chrome’s configured profile only.
- Download detection, PDF validation, renaming, and CSV updates: work with any browser downloading into the watched Downloads folder.
- Other browsers fall back to manual page interaction.

To use Safari, Edge, or Firefox safely, set:

```python
AUTOMATE_PUBLISHER_PDF_CLICK = False
AUTO_CLEAR_BROWSER_CACHE = False
CLOSE_COMPLETED_AUTOMATIC_TABS = False
CLOSE_SCIENCEDIRECT_TABS_AT_BREAK = False
```

The 60-second break still works independently of the browser.