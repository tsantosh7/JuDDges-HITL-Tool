#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup


DEFAULT_INPUT_DIR = "data/national_archive/xml"
DEFAULT_OUTPUT_FILE = "data/national_archive/england_wales_data_refined.jsonl"


def text_or_none(node) -> str | None:
    if not node:
        return None
    value = node.get_text(" ", strip=True)
    return value or None


def attr_or_none(node, attr: str) -> str | None:
    if not node:
        return None
    value = node.get(attr)
    return value or None


def extract_appeal_type(text: str) -> str | None:
    patterns = [
        (r"appeal\s+against\s+\S+\s+sentence\s+or\s+\S+\s+conviction", "conviction_sentence"),
        (r"appeal\s+against\s+\S+\s+conviction\s+or\s+\S+\s+sentence", "conviction_sentence"),
        (r"appeal\s+against\s+\S+\s+conviction", "conviction"),
        (r"appeal\s+against\s+\S+\s+sentence", "sentence"),
    ]
    for pattern, appeal_type in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return appeal_type
    return None


def extract_appeal_outcome(text: str) -> str | None:
    outcome_patterns = {
        "granted": r"appeal\s+is\s+granted",
        "dismissed": r"appeal\s+is\s+dismissed",
        "refused": r"appeal\s+is\s+refused",
        "allowed": r"appeal\s+is\s+allowed",
    }
    for outcome, pattern in outcome_patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            return outcome
    return None


def extract_and_clean_judges(paragraphs) -> list[str]:
    judges: list[str] = []
    for para in paragraphs:
        text = para.get_text(" ", strip=True)
        if re.search(r"\bJustice\b|\bJudge\b|\bSIR\b|\bHonour\b|\bHHJ\b", text, re.IGNORECASE):
            cleaned_text = re.sub(r"\([^)]*\)", "", text).strip()
            cleaned_text = re.sub(r"-.*", "", cleaned_text).strip()
            if cleaned_text and "Royal Courts of Justice" not in cleaned_text and cleaned_text != "THE LORD CHIEF JUSTICE OF ENGLAND AND WALES":
                judges.append(cleaned_text)
    return judges


def categorize_court(court_name: str) -> str:
    name = court_name.upper()
    if "COURT_OF_APPEAL" in name and "CRIMINAL" in name:
        return "court_of_appeal_criminal_division"
    if "EWCA" in name and "CRIMINAL" in name:
        return "court_of_appeal_criminal_division"
    if "COURT_OF_APPEAL" in name:
        return "court_of_appeal_criminal_division"
    if "SUPREME_COURT" in name:
        return "supreme_court"
    if "HIGH_COURT" in name and "ADMINISTRATIVE_COURT" in name:
        return "high_court_administrative_court"
    if "HIGH_COURT" in name and "DIVISIONAL_COURT" in name:
        return "high_court_division_court"
    if "HIGH_COURT" in name:
        return "high_court"
    if "CIVIL_AND_CRIMINAL" in name:
        return "civil_criminal_court"
    if "MARTIAL" in name:
        return "martial_court"
    if "DIVISIONAL_COURT" in name:
        return "division_court"
    return "crown_court"


def unique_clean(values: list[str]) -> list[str] | None:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(str(value).split())
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out or None


def null_if_empty(value: Any) -> Any:
    return value if value else None


def extract_information_from_xml(xml_content: str, file_name: str) -> dict[str, Any]:
    soup = BeautifulSoup(xml_content, "xml")

    citation = text_or_none(soup.find("uk:cite"))
    if not citation:
        cite_node = soup.find(lambda tag: getattr(tag, "name", "") and tag.name.endswith(":cite"))
        citation = text_or_none(cite_node)

    document_id = text_or_none(soup.find("uk:hash"))
    if not document_id:
        hash_node = soup.find(lambda tag: getattr(tag, "name", "") and tag.name.endswith(":hash"))
        document_id = text_or_none(hash_node)

    signature = citation.split("] ", 1)[1].replace(" ", "_") if citation and "] " in citation else None
    hearing_date = text_or_none(soup.find("hearingdate"))
    publication_date = attr_or_none(soup.find("FRBRdate", {"name": "judgment"}), "date")
    date = hearing_date.strip() if hearing_date else publication_date

    frbr_name = attr_or_none(soup.find("FRBRWork").find("FRBRname") if soup.find("FRBRWork") else None, "value")

    court_type_tags = soup.find_all("courtType")
    court_type_names = {
        re.sub(r"\([^)]*\)", "", tag.get_text(" ", strip=True)).replace(" ", "_")
        for tag in court_type_tags
    }
    court_type_raw = re.sub(r"_+", "_", "_".join(sorted(court_type_names))).strip("_")
    court_type = categorize_court(court_type_raw)

    header_text = soup.header.get_text(" ", strip=True) if soup.header else ""
    excerpt = header_text[:500]

    header_content = soup.header.get_text("\n", strip=True) if soup.header else ""
    judgment_body = soup.find("judgmentBody")
    judgment_body_content = judgment_body.get_text("\n", strip=True) if judgment_body else ""
    content = f"{header_content}\n{judgment_body_content}".strip()

    judges = [
        judge["showAs"]
        for judge in soup.find_all("TLCPerson")
        if "showAs" in judge.attrs
        and re.search(r"\bJustice\b|\bJudge\b|\bSIR\b|\bHonour\b|\bHHJ\b", judge["showAs"], re.IGNORECASE)
    ]
    if not judges:
        judges = [judge.get_text(" ", strip=True) for judge in soup.find_all("judge")]
    if not judges and soup.header:
        judges = extract_and_clean_judges(soup.header.find_all("p"))
    if not judges:
        judges.extend(extract_and_clean_judges(soup.find_all("p", style=lambda x: x and "text-align:center" in x)))
    if not judges:
        judges.extend(extract_and_clean_judges(soup.find_all("p", style=lambda x: x and "text-align:right" in x)))
    judges = [
        judge
        for judge in judges
        if re.search(r"\bJustice\b|\bJudge\b|\bSIR\b|\bHonour\b|\bHHJ\b", judge, re.IGNORECASE)
    ]

    manifestation = soup.find("FRBRManifestation")
    work = soup.find("FRBRWork")
    xml_uri = attr_or_none(manifestation.find("FRBRuri") if manifestation else None, "value")
    uri = attr_or_none(work.find("FRBRuri") if work else None, "value")

    legislation = unique_clean([tag.get_text(" ", strip=True) for tag in soup.find_all("ref", {"uk:type": "legislation"})])
    case_references = unique_clean([tag.get_text(" ", strip=True) for tag in soup.find_all("ref", {"uk:type": "case"})])

    case_numbers: set[str] = {tag.get_text(" ", strip=True) for tag in soup.find_all("docketNumber") if tag.get_text(" ", strip=True)}
    case_no_pattern = re.compile(r"Case No:\s*(.*)", re.IGNORECASE)
    for tag in soup.find_all("p", class_="CoverText"):
        match = case_no_pattern.search(tag.get_text(" ", strip=True))
        if match:
            case_numbers.update(num.strip() for num in match.group(1).split(",") if num.strip())
    if not case_numbers:
        right_aligned = soup.find_all("p", style=lambda x: x and "text-align:right" in x)
        fallback_pattern = re.compile(r"\b\d{4}/\d{4}/\w+\b|\d{6}")
        for tag in right_aligned:
            case_numbers.update(fallback_pattern.findall(tag.get_text(" ", strip=True)))

    return {
        "_id": null_if_empty(document_id),
        "citation": null_if_empty(citation),
        "signature": null_if_empty(signature),
        "date": null_if_empty(date),
        "publicationDate": null_if_empty(publication_date),
        "type": null_if_empty(court_type),
        "title": null_if_empty(frbr_name),
        "excerpt": null_if_empty(excerpt),
        "content": null_if_empty(content),
        "judges": unique_clean(judges),
        "caseNumbers": unique_clean(list(case_numbers)),
        "citation_references": case_references,
        "legislation": legislation,
        "file_name": null_if_empty(file_name),
        "appeal_type": null_if_empty(extract_appeal_type(content)),
        "appeal_outcome": null_if_empty(extract_appeal_outcome(content)),
        "xml_uri": null_if_empty(xml_uri),
        "uri": null_if_empty(uri),
        "source": "national_archives",
    }


def process_file(file_path: str) -> tuple[str, dict[str, Any] | None, str | None]:
    try:
        with open(file_path, "r", encoding="utf-8") as xml_file:
            xml_content = xml_file.read()
        return file_path, extract_information_from_xml(xml_content, os.path.basename(file_path)), None
    except Exception as exc:
        return file_path, None, str(exc)


def walk_xml_files(directory_path: Path) -> list[str]:
    return sorted(str(path) for path in directory_path.rglob("*.xml"))


def process_directory(directory_path: Path, output_file: Path, workers: int) -> int:
    xml_files = walk_xml_files(directory_path)
    if not xml_files:
        raise RuntimeError(f"No .xml files found under {directory_path}")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    errors: list[tuple[str, str]] = []

    with output_file.open("w", encoding="utf-8") as jsonl_file:
        with ProcessPoolExecutor(max_workers=max(workers, 1)) as pool:
            futures = [pool.submit(process_file, path) for path in xml_files]
            for i, future in enumerate(as_completed(futures), start=1):
                path, judgment_data, error = future.result()
                if error:
                    errors.append((path, error))
                elif judgment_data:
                    jsonl_file.write(json.dumps(judgment_data, ensure_ascii=False) + "\n")
                    written += 1

                if i % 100 == 0 or i == len(xml_files):
                    print(f"Processed {i}/{len(xml_files)} XML files; written={written}; errors={len(errors)}")

    if errors:
        for path, error in errors[:20]:
            print(f"ERROR {path}: {error}", file=sys.stderr)
        if len(errors) > 20:
            print(f"... {len(errors) - 20} more errors omitted", file=sys.stderr)
        raise RuntimeError(f"Failed to parse {len(errors)} XML files")

    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract HITL fields from National Archives judgment XML into JSONL.")
    parser.add_argument("--input", default=DEFAULT_INPUT_DIR, help="Directory containing downloaded XML files")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE, help="Refined JSONL output path")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    written = process_directory(Path(args.input), Path(args.output), args.workers)
    print(f"Extraction complete. Wrote {written} records to {args.output}.")


if __name__ == "__main__":
    main()
