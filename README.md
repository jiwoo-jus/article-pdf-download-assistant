# Article PDF Download Assistant

This tool helps turn a CSV of PubMed IDs into:

1. A metadata-enriched CSV
2. A folder of downloaded article PDFs

It is not a fully automatic downloader. It opens article pages, detects completed downloads, renames PDFs by PMID, and updates the CSV. For many publisher pages, you may still need to manually click the PDF or Download button.

This tool does not bypass paywalls. It only works for articles you can already access in your browser, so make sure you are logged in through OSU or OSU Library access in Chrome before starting.

---

## 1. Install Requirements

```bash
pip install requests
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

After running the scripts, the CSV will be updated with metadata and download information, including:

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

To download only selected rows, add or edit the `is_fetch_target` column and mark target rows with `Y`:

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

## 6. Configure Chrome

Before running the downloader, open Chrome and make sure you are logged in through OSU or OSU Library access.

Chrome should download files to:

```text
/Users/{your_username}/Downloads
```

For example:

```text
/Users/park.3620/Downloads
```

Check this in:

```text
chrome://settings/downloads
```

Make sure:

* Download location is set to your Downloads folder
* “Ask where to save each file before downloading” is turned off

The script works best when PDFs download directly. If a PDF opens in the browser viewer instead, manually click the PDF or download button.

---

## 7. Download PDFs

Run:

```bash
python download_browser_pdfs.py
```

For each target row, the script will:

1. Read the PMID and DOI from `target_records.csv`
2. Open a likely article or PDF URL in Chrome
3. Wait for a PDF download
4. Let you manually click PDF/Download if needed
5. Detect the completed file in your Downloads folder
6. Move and rename it as:

```text
browser/{pmid}.pdf
```

For example:

```text
browser/37313651.pdf
```

7. Update `target_records.csv` with the download status and output path

During each record, the terminal will show:

```text
[WAIT] 600s | press 's' + Enter to skip
```

Press `s` and Enter to skip the current record.

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
success
skipped
failed
```

The script automatically skips rows that already have a matching file at:

```text
browser/{pmid}.pdf
```

---

## 9. Batch Settings (optional)

You can adjust these constants near the top of `download_browser_pdfs.py`:

```python
BATCH_START = 0
BATCH_LIMIT = 1000
WAIT_TIMEOUT_SECONDS = 600
OPEN_DELAY_SECONDS = 0.5
```

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

* Click the PDF or Download button manually
* Confirm Chrome downloads to `/Users/{your_username}/Downloads`
* Confirm you are logged in through OSU or OSU Library access
* Confirm the article is accessible in your browser
* Press `r` to retry after timeout, or `s` and Enter to skip

### Wrong file was moved

Avoid downloading unrelated files while the script is running. The script moves the newest completed file in your Downloads folder after opening each article page.

### Clear cache

For Elsevier / ScienceDirect, if a download error occurs, it may be because the site detects abnormal or repeated downloading behavior. In that case, try clearing your browser cache from time to time. I use a Chrome extension called “Clear Cache” for this.

---

## 11. Manual Download
If some files are skipped after running the code, you can try finding them manually through OSU Library or https://www.sci-hub.ru/ using DOI in metadata.