#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse


DEFAULT_INPUT = "data/national_archive/england_wales_data_refined.jsonl"
DEFAULT_OUTPUT = "data/national_archive/normalised_new_data.jsonl"
DEFAULT_KNOWN_JSONL = "data/normalised_data.jsonl"


def normalise_canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.netloc == "caselaw.nationalarchives.gov.uk" and parsed.path.startswith("/id/"):
            parsed = parsed._replace(path=parsed.path.replace("/id/", "/", 1))
        return urlunparse(parsed._replace(query="", fragment=""))
    except Exception:
        return url


def load_known(path: str | None) -> tuple[set[str], set[str]]:
    known_ids: set[str] = set()
    known_urls: set[str] = set()
    if not path:
        return known_ids, known_urls
    known_path = Path(path)
    if not known_path.exists():
        return known_ids, known_urls
    with known_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("document_id"):
                known_ids.add(str(obj["document_id"]))
            url = normalise_canonical_url(obj.get("canonical_url") or obj.get("uri"))
            if url:
                known_urls.add(url)
    return known_ids, known_urls


def map_record_to_normalised(record: dict[str, Any]) -> dict[str, Any]:
    canonical_url = normalise_canonical_url(record.get("canonical_url") or record.get("uri"))
    title = record.get("citation")
    case_name = record.get("title")
    if case_name and title:
        title = f"{title} - {case_name}"
    elif case_name:
        title = case_name

    return {
        "document_id": record.get("document_id") or record.get("_id"),
        "canonical_url": canonical_url,
        "published_date": record.get("published_date") or record.get("publicationDate") or record.get("date"),
        "doc_type": record.get("type") or record.get("court"),
        "title": title,
        "excerpt": record.get("excerpt") or record.get("summary"),
        "content_text": record.get("content_text") or record.get("content"),
        "source": record.get("source") or "national_archives",
        "metadata": {
            "citation": record.get("citation"),
            "signature": record.get("signature"),
            "xml_uri": record.get("xml_uri"),
            "file_name": record.get("file_name"),
            "judges": record.get("judges"),
            "caseNumbers": record.get("caseNumbers"),
            "citation_references": record.get("citation_references"),
            "legislation": record.get("legislation"),
            "appeal_type": record.get("appeal_type"),
            "appeal_outcome": record.get("appeal_outcome"),
            "case_name": record.get("title"),
        },
    }


def is_valid(record: dict[str, Any]) -> bool:
    return bool(record.get("document_id") and record.get("canonical_url") and record.get("content_text"))


def normalise_jsonl(input_path: Path, output_path: Path, known_jsonl: str | None, include_known: bool) -> None:
    known_ids, known_urls = (set(), set()) if include_known else load_known(known_jsonl)
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    total = 0
    written = 0
    skipped_known = 0
    skipped_invalid = 0
    skipped_duplicate = 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with input_path.open("r", encoding="utf-8") as infile, output_path.open("w", encoding="utf-8") as outfile:
        for line_number, line in enumerate(infile, start=1):
            if not line.strip():
                continue
            total += 1
            try:
                source_record = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"Skipping invalid JSON on line {line_number}: {exc}")
                skipped_invalid += 1
                continue

            normalised = map_record_to_normalised(source_record)
            document_id = str(normalised.get("document_id") or "")
            canonical_url = str(normalised.get("canonical_url") or "")

            if not is_valid(normalised):
                skipped_invalid += 1
                continue
            if document_id in seen_ids or canonical_url in seen_urls:
                skipped_duplicate += 1
                continue
            if document_id in known_ids or canonical_url in known_urls:
                skipped_known += 1
                continue

            seen_ids.add(document_id)
            seen_urls.add(canonical_url)
            outfile.write(json.dumps(normalised, ensure_ascii=False) + "\n")
            written += 1

    print("Normalisation complete")
    print(f"Input records: {total}")
    print(f"Wrote ingest records: {written}")
    print(f"Skipped known: {skipped_known}")
    print(f"Skipped duplicates: {skipped_duplicate}")
    print(f"Skipped invalid: {skipped_invalid}")
    print(f"Output: {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert refined National Archives JSONL to HITL ingest JSONL.")
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--known-jsonl", default=DEFAULT_KNOWN_JSONL)
    parser.add_argument("--include-known", action="store_true", help="Do not skip records already present in --known-jsonl")
    args = parser.parse_args()
    normalise_jsonl(Path(args.input), Path(args.output), args.known_jsonl, args.include_known)


if __name__ == "__main__":
    main()
