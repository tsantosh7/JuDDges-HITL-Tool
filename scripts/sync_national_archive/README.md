# National Archives Sync

This folder refreshes the Court of Appeal Criminal Division corpus from Find Case Law.

The current local corpus in `data/normalised_data.jsonl` has 6,154 records. The downloader checks the live National Archives search result count, skips canonical URLs already present in that JSONL, downloads only missing XML files, and then creates an ingest JSONL that can be posted through the existing API endpoint.

Run from the repository root:

```bash
python3 scripts/sync_national_archive/00_download_judgements.py
python3 scripts/sync_national_archive/01_extract_jsonl_refined.py
python3 scripts/sync_national_archive/03_normalise_for_ingest.py
python3 scripts/ingest_jsonl.py \
  --file data/national_archive/normalised_new_data.jsonl \
  --api http://localhost:8000 \
  --solr http://localhost:8983/solr \
  --core hitl_test \
  --batch 250 \
  --final-solr-commit
```

Useful options:

- `00_download_judgements.py --include-known --force` rebuilds all downloaded XML instead of only missing records.
- `00_download_judgements.py --max-docs 20` is useful for a smoke test.
- `03_normalise_for_ingest.py --include-known` emits all extracted records instead of only records missing from `data/normalised_data.jsonl`.
