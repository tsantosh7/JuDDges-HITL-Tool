#!/usr/bin/env python3
import os
import re
import json
import argparse
from typing import Any, Dict, List, Optional, Tuple, Iterable


import psycopg
import requests

import sys
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

from training.normalize_output2 import convert_output1_to_output2 as normalize_output2



DNA_DEFAULT = "data not available"


def coerce_db_url(db_url: str) -> str:
    if db_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + db_url[len("postgresql+psycopg://"):]
    if db_url.startswith("postgresql+psycopg2://"):
        return "postgresql://" + db_url[len("postgresql+psycopg2://"):]
    return db_url


def canon_key(s: str) -> str:
    s = (s or "").strip().lower()
    if s.startswith("#"):
        s = s[1:]
    s = re.sub(r"^(code|tag)\s*[:=]\s*", "", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def clean_value(s: str) -> str:
    s = (s or "").replace("\xa0", " ").strip()
    s = re.sub(r"[\r\n]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_tags(tags_json: Any) -> List[str]:
    """
    hypothesis_annotations.tags is json in Postgres.
    psycopg typically returns JSON as Python dict/list already.
    Hypothesis commonly stores tags as a list of strings.
    """
    if tags_json is None:
        return []
    if isinstance(tags_json, list):
        return [str(x).strip() for x in tags_json if x is not None and str(x).strip()]
    if isinstance(tags_json, tuple):
        return [str(x).strip() for x in tags_json if x is not None and str(x).strip()]
    if isinstance(tags_json, dict):
        # uncommon, but handle just in case
        # e.g., {"tags":[...]}
        v = tags_json.get("tags")
        if isinstance(v, list):
            return [str(x).strip() for x in v if x is not None and str(x).strip()]
        return []
    if isinstance(tags_json, str):
        s = tags_json.strip()
        if not s:
            return []
        try:
            obj = json.loads(s)
            return parse_tags(obj)
        except Exception:
            # fallback: split
            return [p for p in re.split(r"[,\s]+", s) if p]
    return []

def build_blob_and_kv_from_code_map(code_map: Dict[str, List[str]], sentinel: str) -> Tuple[str, List[str]]:
    kv: List[str] = []
    lines: List[str] = []

    for code, vals in (code_map or {}).items():
        if not vals:
            continue
        real_vals = [v for v in vals if v and v != "__PRESENT__" and v.lower() != sentinel.lower()]
        if not real_vals:
            continue
        for v in real_vals:
            kv.append(f"{code}={v}")
        lines.append(f"{code}: " + " | ".join(real_vals))

    return "\n".join(lines), kv


def chunked(items: List[Dict[str, Any]], n: int) -> Iterable[List[Dict[str, Any]]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def solr_atomic_update(solr_base: str, core: str, docs: List[Dict[str, Any]], commit_within_ms: int = 5000) -> None:
    url = f"{solr_base.rstrip('/')}/{core}/update"
    params = {"commitWithin": str(commit_within_ms), "wt": "json"}
    r = requests.post(url, params=params, json=docs, timeout=120)
    r.raise_for_status()


def pick_first_existing_col(cols: set, candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c in cols:
            return c
    return None


def build_code_maps(conn) -> Tuple[Dict[str, str], set]:
    """
    Build alias->canonical-code mapping using codes + code_aliases tables.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='codes'
        """)
        code_cols = {r[0] for r in cur.fetchall()}

        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='code_aliases'
        """)
        alias_cols = {r[0] for r in cur.fetchall()}

    code_key_col = pick_first_existing_col(code_cols, ["code", "code_key", "key", "name"])
    if not code_key_col:
        raise RuntimeError(f"Couldn't find code key column in codes. cols={sorted(code_cols)}")

    alias_col = pick_first_existing_col(alias_cols, ["alias", "alias_key", "alias_text", "text", "from_alias"])
    if not alias_col:
        raise RuntimeError(f"Couldn't find alias column in code_aliases. cols={sorted(alias_cols)}")

    code_ref_col = pick_first_existing_col(alias_cols, ["code", "code_key", "code_name", "to_code"])
    if not code_ref_col:
        raise RuntimeError(f"Couldn't find code ref column in code_aliases. cols={sorted(alias_cols)}")

    alias_to_code: Dict[str, str] = {}
    valid_codes: set = set()

    with conn.cursor() as cur:
        cur.execute(f"SELECT {code_key_col} FROM codes")
        for (ck,) in cur.fetchall():
            if isinstance(ck, str) and ck.strip():
                ck = ck.strip()
                valid_codes.add(ck)
                alias_to_code[canon_key(ck)] = ck

        cur.execute(f"SELECT {alias_col}, {code_ref_col} FROM code_aliases")
        for a, ck in cur.fetchall():
            if not a or not ck:
                continue
            a = str(a).strip()
            ck = str(ck).strip()
            if not a or not ck:
                continue
            alias_to_code[canon_key(a)] = ck

    return alias_to_code, valid_codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default="hitl_test")
    ap.add_argument("--solr_base", default=os.getenv("SOLR_BASE_URL", "http://localhost:8983/solr"))
    ap.add_argument("--db_url", default=os.getenv("DATABASE_URL"))
    ap.add_argument("--project_id", default=None)
    ap.add_argument("--group_id", default=None)
    ap.add_argument("--sentinel", default=DNA_DEFAULT)
    ap.add_argument("--batch_size", type=int, default=300)
    ap.add_argument("--commit_within_ms", type=int, default=5000)
    ap.add_argument("--limit_docs", type=int, default=0)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if not args.db_url:
        raise SystemExit("Missing DATABASE_URL (or pass --db_url)")
    db_url = coerce_db_url(args.db_url)

    # doc_id -> code -> list(values)
    doc_to_values: Dict[str, Dict[str, List[str]]] = {}
    docs_seen: set = set()

    with psycopg.connect(db_url) as conn:
        alias_to_code, valid_codes = build_code_maps(conn)

        # Optional project scope join
        join = ""
        where = []
        params = []

        if args.project_id:
            join = "JOIN project_documents pd ON pd.document_id = ha.document_id"
            where.append("pd.project_id = %s")
            params.append(args.project_id)

        if args.group_id:
            where.append("ha.group_id = %s")
            params.append(args.group_id)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        role_filter = """
            COALESCE(hg.is_exportable, TRUE) = TRUE
            AND COALESCE(hg.group_role, 'human_workspace') IN ('human_workspace', 'gold')
        """
        if where:
            where_sql = "WHERE " + role_filter + " AND " + " AND ".join(where)
        else:
            where_sql = "WHERE " + role_filter

        sql = f"""
            SELECT ha.document_id, ha.tags, ha.text, ha.exact
            FROM hypothesis_annotations ha
            JOIN hypothesis_groups hg ON hg.group_id = ha.group_id
            {join}
            {where_sql}
        """

        n_rows = 0
        n_mapped = 0

        with conn.cursor() as cur:
            cur.execute(sql, params)
            for (doc_id, tags_json, text_val, exact_val) in cur:
                n_rows += 1
                if not doc_id:
                    continue
                doc_id = str(doc_id).strip()
                if not doc_id:
                    continue

                # optional doc limit
                if args.limit_docs and doc_id not in docs_seen:
                    if len(docs_seen) >= args.limit_docs:
                        continue
                    docs_seen.add(doc_id)

                tags = parse_tags(tags_json)
                if not tags:
                    continue

                # Prefer human comment text; fallback to selected snippet (exact)
                v_text = clean_value(text_val if isinstance(text_val, str) else (str(text_val) if text_val is not None else ""))
                v_exact = clean_value(exact_val if isinstance(exact_val, str) else (str(exact_val) if exact_val is not None else ""))

                value = v_text or v_exact  # may be empty

                for t in tags:
                    ck = alias_to_code.get(canon_key(t))
                    if not ck:
                        continue
                    if ck not in valid_codes:
                        continue

                    per_doc = doc_to_values.setdefault(doc_id, {})
                    per_code = per_doc.setdefault(ck, [])

                    if value and value.lower() != args.sentinel.lower():
                        if value not in per_code:
                            per_code.append(value)
                    else:
                        # presence-only marker
                        if "__PRESENT__" not in per_code:
                            per_code.append("__PRESENT__")

                    n_mapped += 1

        if args.debug:
            print(f"Read {n_rows} hypothesis rows; mapped {n_mapped} tag->code assignments.")
            # helpful to see tag forms
            # (sample a few raw tags seen)
            # Note: we don't have tags cached here without extra work; keep debug minimal.

    if not doc_to_values:
        print("No human values found to index (check tags mapping / codes + code_aliases).")
        return

    atomic_docs: List[Dict[str, Any]] = []
    for doc_id, code_map in doc_to_values.items():
        kv: List[str] = []
        codes_present: List[str] = []
        lines: List[str] = []

        for code, vals in code_map.items():
            if not vals:
                continue
            codes_present.append(code)

            real_vals = [v for v in vals if v != "__PRESENT__"]
            if real_vals:
                for v in real_vals:
                    kv.append(f"{code}={v}")
                lines.append(f"{code}: " + " | ".join(real_vals))
            else:
                lines.append(f"{code}: [tagged]")

        # RAW (output_1 style dict)
        out1: Dict[str, List[str]] = {}
        for code, vals in code_map.items():
            if not vals:
                continue
            # keep "__PRESENT__" out of out1 (it breaks/poisons value normalisation)
            real_vals = [v for v in vals if v != "__PRESENT__"]
            if real_vals:
                out1[code] = real_vals

        raw_blob, raw_kv = build_blob_and_kv_from_code_map(code_map, args.sentinel)

        # NORMALISED (output_2)
        out2 = normalize_output2(out1) if out1 else {}
        norm_codes_present = sorted(out2.keys())
        norm_blob, norm_kv = build_blob_and_kv_from_code_map(out2, args.sentinel)

        atomic_docs.append({
            "document_id_s": doc_id,
            "codes_present_human_ss": {"set": sorted(set(codes_present))},
            "code_value_human_kv_ss": {"set": raw_kv},
            "values_human_txt": {"set": raw_blob},

            # NEW: normalised value search + KV
            "code_value_human_norm_kv_ss": {"set": norm_kv},
            "values_human_norm_txt": {"set": norm_blob},
        })

    pushed = 0
    for batch in chunked(atomic_docs, args.batch_size):
        solr_atomic_update(args.solr_base, args.core, batch, commit_within_ms=args.commit_within_ms)
        pushed += len(batch)
        print(f"Pushed {pushed}/{len(atomic_docs)} docs to Solr...")

    print(f"Done. Updated {pushed} docs in Solr core={args.core} (human values).")


if __name__ == "__main__":
    main()
