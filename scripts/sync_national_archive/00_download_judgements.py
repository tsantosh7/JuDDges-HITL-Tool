#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://caselaw.nationalarchives.gov.uk"
DEFAULT_COURT = "ewca/crim"
DEFAULT_KNOWN_JSONL = "data/normalised_data.jsonl"
DEFAULT_OUTPUT_DIR = "data/national_archive/xml"
DEFAULT_MANIFEST = "data/national_archive/judgments_manifest.csv"


@dataclass(frozen=True)
class SearchResult:
    title: str
    link: str
    date: str
    citation: str


def normalise_canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc == "caselaw.nationalarchives.gov.uk" and parsed.path.startswith("/id/"):
        return urlunparse(parsed._replace(path=parsed.path.replace("/id/", "/", 1)))
    return urlunparse(parsed._replace(query="", fragment=""))


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": "hitl-tool-national-archive-sync/1.0 "
            "(https://github.com/stirunag/hitl-tool)"
        }
    )
    return session


def search_url(court: str, order: str, per_page: int, page: int) -> str:
    query = urlencode(
        {
            "query": "",
            "court": court,
            "order": order,
            "per_page": str(per_page),
            "page": str(page),
        }
    )
    return f"{BASE_URL}/search?{query}"


def parse_total_documents(soup: BeautifulSoup) -> int | None:
    node = soup.select_one(".results__results-intro")
    if not node:
        return None
    match = re.search(r"([\d,]+)\s+documents?\s+found", node.get_text(" ", strip=True))
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def clean_text(value: str) -> str:
    return " ".join(value.split())


def parse_search_page(html: str) -> tuple[int | None, list[SearchResult]]:
    soup = BeautifulSoup(html, "html.parser")
    total = parse_total_documents(soup)
    rows: list[SearchResult] = []

    for body in soup.select("div.documents-table tbody"):
        link_node = body.select_one("a[href]")
        if not link_node:
            continue

        href = (link_node.get("href") or "").split("?", 1)[0]
        if not href.startswith("/"):
            continue

        title = clean_text(link_node.get_text(" ", strip=True))
        link = normalise_canonical_url(urljoin(BASE_URL, href)) or urljoin(BASE_URL, href)
        citation = ""
        date = ""

        for cell in body.find_all("td"):
            text = clean_text(cell.get_text(" ", strip=True))
            if text.startswith("Neutral citation"):
                citation = text.replace("Neutral citation", "", 1).strip()
            elif text.startswith("Handed down"):
                date = text.replace("Handed down", "", 1).strip()

        rows.append(SearchResult(title=title, link=link, date=date, citation=citation))

    return total, rows


def fetch_page(session: requests.Session, court: str, order: str, per_page: int, page: int, timeout: int) -> tuple[int | None, list[SearchResult]]:
    response = session.get(search_url(court, order, per_page, page), timeout=timeout)
    response.raise_for_status()
    return parse_search_page(response.text)


def load_known_urls(path: str | None) -> set[str]:
    if not path:
        return set()

    known_path = Path(path)
    if not known_path.exists():
        return set()

    known: set[str] = set()
    import json

    with known_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = normalise_canonical_url(obj.get("canonical_url") or obj.get("uri"))
            if url:
                known.add(url)
    return known


def safe_xml_name(link: str) -> str:
    parsed = urlparse(link)
    stem = parsed.path.strip("/").replace("/", "_")
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("_")
    return f"{stem}.xml"


def write_manifest(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "title",
        "citation",
        "date",
        "link",
        "xml_url",
        "xml_path",
        "status",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def download_xml(
    item: SearchResult,
    output_dir: Path,
    session: requests.Session,
    timeout: int,
    force: bool,
    request_delay: float,
) -> dict:
    xml_url = f"{item.link.rstrip('/')}/data.xml"
    output_path = output_dir / safe_xml_name(item.link)

    row = {
        "title": item.title,
        "citation": item.citation,
        "date": item.date,
        "link": item.link,
        "xml_url": xml_url,
        "xml_path": str(output_path),
        "status": "",
        "error": "",
    }

    if output_path.exists() and not force:
        row["status"] = "exists"
        return row

    if request_delay > 0:
        time.sleep(request_delay)

    try:
        response = session.get(xml_url, timeout=timeout)
        response.raise_for_status()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        tmp_path.write_bytes(response.content)
        tmp_path.replace(output_path)
        row["status"] = "downloaded"
    except Exception as exc:
        row["status"] = "error"
        row["error"] = str(exc)
    return row


def collect_results(args: argparse.Namespace, session: requests.Session) -> tuple[int, list[SearchResult]]:
    total, first_page = fetch_page(session, args.court, args.order, args.per_page, 1, args.timeout)
    if total is None:
        raise RuntimeError("Could not find the total document count on the National Archives search page")

    page_count = math.ceil(total / args.per_page)
    if args.max_pages:
        page_count = min(page_count, args.max_pages)

    results = list(first_page)
    print(f"National Archives reports {total:,} documents for court={args.court}.")
    print(f"Scraping {page_count:,} result pages at {args.per_page} documents/page.")

    for page in range(2, page_count + 1):
        _, page_results = fetch_page(session, args.court, args.order, args.per_page, page, args.timeout)
        results.extend(page_results)
        if page % 10 == 0 or page == page_count:
            print(f"Scraped page {page}/{page_count}; discovered {len(results):,} links.")

    deduped: dict[str, SearchResult] = {}
    for item in results:
        deduped[item.link] = item
    return total, list(deduped.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="Download National Archives Find Case Law judgment XML.")
    parser.add_argument("--court", default=DEFAULT_COURT, help="Court filter, e.g. ewca/crim")
    parser.add_argument("--order", default="-date", choices=("-date", "date"), help="Search order")
    parser.add_argument("--per-page", type=int, default=50, choices=(10, 25, 50))
    parser.add_argument("--max-pages", type=int, default=0, help="Limit pages scraped; 0 means all")
    parser.add_argument("--max-docs", type=int, default=0, help="Limit XML downloads after filtering; 0 means all")
    parser.add_argument("--known-jsonl", default=DEFAULT_KNOWN_JSONL, help="Existing ingest JSONL used to skip known canonical URLs")
    parser.add_argument("--include-known", action="store_true", help="Download records even if already present in --known-jsonl")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--request-delay", type=float, default=0.05)
    parser.add_argument("--force", action="store_true", help="Re-download XML files that already exist")
    args = parser.parse_args()

    session = build_session()
    total, all_results = collect_results(args, session)

    known = set() if args.include_known else load_known_urls(args.known_jsonl)
    candidates = [item for item in all_results if item.link not in known]
    skipped_known = len(all_results) - len(candidates)
    if args.max_docs:
        candidates = candidates[: args.max_docs]

    print(f"Discovered {len(all_results):,} unique result links from {total:,} reported documents.")
    if known:
        print(f"Skipping {skipped_known:,} links already present in {args.known_jsonl}.")
    print(f"Downloading {len(candidates):,} XML files into {args.output_dir}.")

    output_dir = Path(args.output_dir)
    manifest_path = Path(args.manifest)
    rows: list[dict] = []

    if candidates:
        with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as pool:
            future_to_item = {
                pool.submit(
                    download_xml,
                    item,
                    output_dir,
                    session,
                    args.timeout,
                    args.force,
                    args.request_delay,
                ): item
                for item in candidates
            }
            for i, future in enumerate(as_completed(future_to_item), start=1):
                rows.append(future.result())
                if i % 50 == 0 or i == len(candidates):
                    print(f"Processed {i}/{len(candidates)} downloads.")

    write_manifest(manifest_path, rows)
    errors = sum(1 for row in rows if row["status"] == "error")
    print(f"Manifest written to {manifest_path}.")
    print(f"Done. downloaded={sum(1 for row in rows if row['status'] == 'downloaded')} exists={sum(1 for row in rows if row['status'] == 'exists')} errors={errors}")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
