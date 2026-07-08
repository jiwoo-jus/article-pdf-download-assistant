"""Fill PubMed metadata into a CSV in place.

Edit INPUT_CSV if your source file lives outside this repository.
The CSV must contain at least a pmid column.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode


PMID_COLUMN = "pmid"
PMCID_COLUMN = "pmcid"
DOI_COLUMN = "doi"
TITLE_COLUMN = "title"
ABSTRACT_COLUMN = "abstract"
JOURNAL_COLUMN = "journal"
YEAR_COLUMN = "year"

METADATA_FIELDS = [
    PMCID_COLUMN,
    DOI_COLUMN,
    TITLE_COLUMN,
    ABSTRACT_COLUMN,
    JOURNAL_COLUMN,
    YEAR_COLUMN,
]

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = (PROJECT_ROOT / "config.yaml").resolve()

# INPUT_CSV = Path("/Users/park.3620/Documents/workspace/onetimeuse/pubmed-pdf-fetch/jiwoo_records.csv").resolve()
INPUT_CSV = (PROJECT_ROOT / "target_records.csv").resolve()
OUTPUT_CSV = INPUT_CSV
TOOL_NAME = "fill_pubmed_metadata.py"

BATCH_SIZE = 200
REQUEST_TIMEOUT_SECONDS = 60
SLEEP_BETWEEN_REQUESTS_SECONDS = 0.34

ENABLE_ESUMMARY = True
ENABLE_ESEARCH_FALLBACK = True

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"



@dataclass
class PubMedRecord:
    pmid: str
    pmcid: str = ""
    doi: str = ""
    title: str = ""
    abstract: str = ""
    journal: str = ""
    year: str = ""


def load_flat_config(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        cleaned_value = value.strip()
        if len(cleaned_value) >= 2 and cleaned_value[0] == cleaned_value[-1] and cleaned_value[0] in {'"', "'"}:
            cleaned_value = cleaned_value[1:-1]
        values[key.strip()] = cleaned_value
    return values


CONFIG = load_flat_config(CONFIG_PATH)
ENTREZ_EMAIL = CONFIG.get("ncbi_email", "")
NCBI_API_KEY = CONFIG.get("ncbi_api_key", "")


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def build_params(**kwargs: str) -> dict[str, str]:
    params = {
        "db": "pubmed",
        "tool": TOOL_NAME,
        "email": ENTREZ_EMAIL,
    }
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    for key, value in kwargs.items():
        if value != "":
            params[key] = value
    return params


def request_text(url: str, params: dict[str, str], method: str = "GET") -> str:
    if method == "GET":
        full_url = f"{url}?{urlencode(params)}"
        request = urllib_request.Request(full_url, method="GET")
    else:
        payload = urlencode(params).encode("utf-8")
        request = urllib_request.Request(url, data=payload, method=method)

    try:
        with urllib_request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error calling {url}: {exc}") from exc


def find_first_article_id(article: ET.Element, id_type: str) -> str:
    for article_id in article.findall(".//ArticleId"):
        if article_id.get("IdType") == id_type and article_id.text:
            return article_id.text.strip()
    return ""


def extract_title(article: ET.Element) -> str:
    title_node = article.find(".//ArticleTitle")
    if title_node is None:
        return ""
    return "".join(title_node.itertext()).strip()


def extract_abstract(article: ET.Element) -> str:
    pieces: list[str] = []
    for abstract_node in article.findall(".//Abstract/AbstractText"):
        text = "".join(abstract_node.itertext()).strip()
        label = (abstract_node.get("Label") or "").strip()
        if not text:
            continue
        if label and not text.lower().startswith(label.lower()):
            pieces.append(f"{label}: {text}")
        else:
            pieces.append(text)
    return " ".join(pieces).strip()


def extract_journal(article: ET.Element) -> str:
    journal_node = article.find(".//Journal/Title")
    if journal_node is not None and journal_node.text:
        return journal_node.text.strip()

    iso_node = article.find(".//Journal/ISOAbbreviation")
    if iso_node is not None and iso_node.text:
        return iso_node.text.strip()

    return ""


def extract_year(article: ET.Element) -> str:
    for xpath in (
        ".//PubDate/Year",
        ".//ArticleDate/Year",
        ".//PubMedPubDate[@PubStatus='pubmed']/Year",
        ".//PubMedPubDate[@PubStatus='entrez']/Year",
    ):
        year_node = article.find(xpath)
        if year_node is not None and year_node.text:
            return year_node.text.strip()
    return ""


def parse_efetch_records(pmids: list[str]) -> dict[str, PubMedRecord]:
    if not pmids:
        return {}

    xml_text = request_text(
        EFETCH_URL,
        build_params(id=",".join(pmids), rettype="xml", retmode="xml"),
        method="POST",
    )
    root = ET.fromstring(xml_text)

    records: dict[str, PubMedRecord] = {}
    for article in root.findall(".//PubmedArticle"):
        pmid_node = article.find(".//MedlineCitation/PMID")
        if pmid_node is None or not pmid_node.text:
            continue
        pmid = pmid_node.text.strip()
        records[pmid] = PubMedRecord(
            pmid=pmid,
            pmcid=find_first_article_id(article, "pmc"),
            doi=find_first_article_id(article, "doi"),
            title=extract_title(article),
            abstract=extract_abstract(article),
            journal=extract_journal(article),
            year=extract_year(article),
        )
    return records


def parse_esummary_records(pmids: list[str]) -> dict[str, dict[str, str]]:
    if not pmids or not ENABLE_ESUMMARY:
        return {}

    raw_json = request_text(
        ESUMMARY_URL,
        build_params(id=",".join(pmids), retmode="json", version="2.0"),
    )
    payload = json.loads(raw_json)
    result = payload.get("result", {})

    records: dict[str, dict[str, str]] = {}
    for pmid in pmids:
        summary = result.get(pmid)
        if not isinstance(summary, dict):
            continue

        article_ids = summary.get("articleids") or []
        doi = ""
        pmcid = ""
        for item in article_ids:
            if not isinstance(item, dict):
                continue
            id_type = str(item.get("idtype") or "").lower()
            value = str(item.get("value") or "").strip()
            if id_type == "doi" and not doi:
                doi = value
            elif id_type == "pmc" and not pmcid:
                pmcid = value

        title = str(summary.get("title") or "").strip()
        journal = str(summary.get("fulljournalname") or summary.get("source") or "").strip()
        pubdate = str(summary.get("pubdate") or "").strip()
        year = pubdate[:4] if len(pubdate) >= 4 and pubdate[:4].isdigit() else ""
        records[pmid] = {
            PMCID_COLUMN: pmcid,
            DOI_COLUMN: doi,
            TITLE_COLUMN: title,
            JOURNAL_COLUMN: journal,
            YEAR_COLUMN: year,
        }
    return records


def search_pmid_by_title(title: str) -> str:
    if not ENABLE_ESEARCH_FALLBACK:
        return ""

    query = title.strip()
    if not query:
        return ""

    raw_json = request_text(
        ESEARCH_URL,
        build_params(term=f'"{query}"[Title]', retmode="json", retmax="1"),
    )
    payload = json.loads(raw_json)
    id_list = payload.get("esearchresult", {}).get("idlist", [])
    if not id_list:
        return ""
    return str(id_list[0]).strip()


def ensure_columns(fieldnames: list[str], required_columns: list[str]) -> list[str]:
    merged = list(fieldnames)
    for column in required_columns:
        if column not in merged:
            merged.append(column)
    return merged


def normalize_cell(value: str | None) -> str:
    return (value or "").strip()


def needs_metadata(row: dict[str, str]) -> bool:
    return any(not normalize_cell(row.get(column)) for column in METADATA_FIELDS)


def collect_target_pmids(rows: list[dict[str, str]]) -> tuple[list[str], int]:
    pmids: list[str] = []
    fallback_needed = 0

    for row in rows:
        if not needs_metadata(row):
            continue

        pmid = normalize_cell(row.get(PMID_COLUMN))
        if pmid:
            pmids.append(pmid)
            continue

        fallback_needed += 1

    return sorted(set(pmids)), fallback_needed


def merge_record_into_row(row: dict[str, str], record: PubMedRecord) -> bool:
    changed = False
    incoming = {
        PMCID_COLUMN: record.pmcid,
        DOI_COLUMN: record.doi,
        TITLE_COLUMN: record.title,
        ABSTRACT_COLUMN: record.abstract,
        JOURNAL_COLUMN: record.journal,
        YEAR_COLUMN: record.year,
    }

    for column, value in incoming.items():
        if value and not normalize_cell(row.get(column)):
            row[column] = value
            changed = True

    return changed


def populate_missing_metadata(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], int, int, int]:
    target_pmids, fallback_needed = collect_target_pmids(rows)
    if not target_pmids and fallback_needed == 0:
        return rows, 0, 0, 0

    target_rows = sum(1 for row in rows if needs_metadata(row))
    fetched_records: dict[str, PubMedRecord] = {}

    for batch in chunked(target_pmids, BATCH_SIZE):
        print(f"Fetching metadata for PMIDs: {batch[0]} - {batch[-1]} (batch size: {len(batch)})")
        efetch_records = parse_efetch_records(batch)
        esummary_records = parse_esummary_records(batch)

        for pmid in batch:
            record = efetch_records.get(pmid, PubMedRecord(pmid=pmid))
            summary = esummary_records.get(pmid, {})
            if not record.pmcid:
                record.pmcid = summary.get(PMCID_COLUMN, "")
            if not record.doi:
                record.doi = summary.get(DOI_COLUMN, "")
            if not record.title:
                record.title = summary.get(TITLE_COLUMN, "")
            if not record.journal:
                record.journal = summary.get(JOURNAL_COLUMN, "")
            if not record.year:
                record.year = summary.get(YEAR_COLUMN, "")
            fetched_records[pmid] = record

        time.sleep(SLEEP_BETWEEN_REQUESTS_SECONDS)

    updated_rows = 0
    unresolved_rows = 0

    for row in rows:
        if not needs_metadata(row):
            continue

        pmid = normalize_cell(row.get(PMID_COLUMN))
        if not pmid and ENABLE_ESEARCH_FALLBACK:
            pmid = search_pmid_by_title(normalize_cell(row.get(TITLE_COLUMN)))
            if pmid:
                row[PMID_COLUMN] = pmid
                if pmid not in fetched_records:
                    record = parse_efetch_records([pmid]).get(pmid, PubMedRecord(pmid=pmid))
                    summary = parse_esummary_records([pmid]).get(pmid, {})
                    if not record.pmcid:
                        record.pmcid = summary.get(PMCID_COLUMN, "")
                    if not record.doi:
                        record.doi = summary.get(DOI_COLUMN, "")
                    if not record.title:
                        record.title = summary.get(TITLE_COLUMN, "")
                    if not record.journal:
                        record.journal = summary.get(JOURNAL_COLUMN, "")
                    if not record.year:
                        record.year = summary.get(YEAR_COLUMN, "")
                    fetched_records[pmid] = record
                time.sleep(SLEEP_BETWEEN_REQUESTS_SECONDS)

        record = fetched_records.get(pmid)
        if record is None:
            unresolved_rows += 1
            continue

        if merge_record_into_row(row, record):
            updated_rows += 1

    return rows, updated_rows, unresolved_rows, target_rows


def write_csv(fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    temp_output = OUTPUT_CSV if OUTPUT_CSV != INPUT_CSV else INPUT_CSV.with_suffix(f"{INPUT_CSV.suffix}.tmp")
    with open(temp_output, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    if temp_output != OUTPUT_CSV:
        os.replace(temp_output, OUTPUT_CSV)
    else:
        os.replace(temp_output, INPUT_CSV)


def main() -> int:
    if not ENTREZ_EMAIL:
        print(
            f"Missing ncbi_email in {CONFIG_PATH}. Copy config.template.yaml to config.yaml and fill it in.",
            file=sys.stderr,
        )
        return 1

    if not INPUT_CSV.exists():
        print(f"Input CSV not found: {INPUT_CSV}", file=sys.stderr)
        return 1

    with open(INPUT_CSV, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    if not rows:
        print("Input CSV is empty.")
        return 0

    if PMID_COLUMN not in fieldnames:
        print(f"Missing required column: {PMID_COLUMN}", file=sys.stderr)
        return 1

    fieldnames = ensure_columns(fieldnames, METADATA_FIELDS)

    try:
        rows, updated_rows, unresolved_rows, target_rows = populate_missing_metadata(rows)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to populate metadata: {exc}", file=sys.stderr)
        return 1

    write_csv(fieldnames, rows)

    print(f"Target rows: {target_rows}")
    print(f"Rows updated: {updated_rows}")
    print(f"Rows unresolved: {unresolved_rows}")
    print(f"Output written to: {OUTPUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())