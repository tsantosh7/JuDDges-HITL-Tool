#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests
from sqlalchemy import create_engine, text


HYPOTHESIS_API_BASE = "https://api.hypothes.is/api"
SENTINEL = "data not available"
REVIEW_REJECT_TAGS = {"review:reject", "review:rejected", "status:rejected", "reject"}
SYSTEM_TAG_PREFIXES = (
    "source:",
    "status:",
    "project_id:",
    "doc_id:",
    "suggestion_id:",
    "gold_ref_id:",
    "anchored:",
    "implicit_accept:",
    "propagation:",
    "example_count:",
)
SYSTEM_TAGS = {"bot:hitl"}


@dataclass
class Example:
    annotation_id: str
    document_id: str
    code: str
    value: str
    exact: str
    prefix: str
    suffix: str


@dataclass
class TargetDoc:
    document_id: str
    canonical_url: str
    content_text: str


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(str(url))
    if parsed.netloc == "caselaw.nationalarchives.gov.uk" and parsed.path.startswith("/id/"):
        parsed = parsed._replace(path=parsed.path.replace("/id/", "/", 1))
    return urlunparse(parsed._replace(query="", fragment=""))


def normalize_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        try:
            parsed = json.loads(tags)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if str(t).strip()]
        except Exception:
            return [tags.strip()] if tags.strip() else []
    if isinstance(tags, (list, tuple)):
        return [str(t).strip() for t in tags if str(t).strip()]
    return []


def candidate_codes_from_tags(tags: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tag in normalize_tags(tags):
        lc = tag.lower()
        code = ""
        if lc.startswith("field:"):
            code = tag.split(":", 1)[1].strip()
        elif lc not in SYSTEM_TAGS and lc not in REVIEW_REJECT_TAGS and not any(lc.startswith(p) for p in SYSTEM_TAG_PREFIXES):
            code = tag.strip()
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def has_reject(tags: Any, text_value: Any = None) -> bool:
    tag_set = {t.lower() for t in normalize_tags(tags)}
    if tag_set & REVIEW_REJECT_TAGS:
        return True
    text_lc = str(text_value or "").strip().lower()
    return text_lc in REVIEW_REJECT_TAGS or text_lc in {"rejected"}


def review_value(text_value: Any, exact: Any) -> str:
    value = str(text_value or "").strip()
    if value and not has_reject([], value):
        return value
    return str(exact or "").strip()


def quote_norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def find_unique_text_quote_selector(content_text: str, value: str, *, window: int = 50, min_len: int = 5) -> dict | None:
    raw_value = str(value or "").strip()
    if not content_text or quote_norm(raw_value) in {"", SENTINEL}:
        return None
    if len(quote_norm(raw_value)) < min_len:
        return None

    content_norm = quote_norm(content_text)
    value_norm = quote_norm(raw_value)
    start = 0
    hits = 0
    while True:
        idx = content_norm.find(value_norm, start)
        if idx == -1:
            break
        hits += 1
        if hits > 1:
            return None
        start = idx + 1
    if hits != 1:
        return None

    pattern = r"\s+".join(re.escape(p) for p in re.split(r"\s+", raw_value) if p)
    matches = list(re.finditer(pattern, content_text, flags=re.IGNORECASE))
    if len(matches) != 1:
        return None
    match = matches[0]
    return {
        "type": "TextQuoteSelector",
        "exact": content_text[match.start():match.end()],
        "prefix": content_text[max(0, match.start() - window):match.start()],
        "suffix": content_text[match.end():min(len(content_text), match.end() + window)],
    }


def stable_suggestion_id(project_id: str, document_id: str, code: str, value: str) -> str:
    raw = json.dumps(
        {"kind": "hypothesis_propagation", "project_id": project_id, "document_id": document_id, "code": code, "value": value},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def hyp_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.hypothesis.v1+json",
        "Content-Type": "application/json;charset=utf-8",
    }


def create_hypothesis_annotation(token: str, payload: dict, timeout: int = 60) -> dict:
    r = requests.post(
        f"{HYPOTHESIS_API_BASE}/annotations",
        headers=hyp_headers(token),
        json=payload,
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Hypothesis create failed: {r.status_code} {r.text[:1000]}")
    return r.json()


def openai_output_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    parts: list[str] = []
    for item in data.get("output") or []:
        for content in item.get("content") or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "\n".join(parts)


def extract_values_with_openai(
    *,
    api_key: str,
    model: str,
    code: str,
    examples: list[Example],
    target: TargetDoc,
    max_doc_chars: int,
    timeout: int,
) -> list[str]:
    example_lines: list[str] = []
    for i, ex in enumerate(examples, start=1):
        context = " ".join(part.strip() for part in [ex.prefix, ex.exact, ex.suffix] if part and part.strip())
        example_lines.append(
            f"Example {i}\n"
            f"Code: {code}\n"
            f"Accepted value: {ex.value}\n"
            f"Highlighted text/context: {context[:1200]}"
        )

    schema = {
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "required": ["values"],
        "additionalProperties": False,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You propagate legal review tags from examples. Return JSON only. "
                "Extract values for the requested code from the target judgment. "
                "Every returned value must be copied exactly from the target judgment. "
                "If the target does not contain evidence for the code, return an empty values array."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Propagate this Hypothesis field/tag: {code}\n\n"
                "Reviewer-approved examples:\n\n"
                + "\n\n".join(example_lines)
                + "\n\nTARGET JUDGMENT:\n"
                + target.content_text[:max_doc_chars]
            ),
        },
    ]
    payload = {
        "model": model,
        "input": messages,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "propagated_values",
                "strict": True,
                "schema": schema,
            }
        },
    }
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            r = requests.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            if r.status_code in {408, 409, 429} or r.status_code >= 500:
                raise RuntimeError(f"OpenAI transient failure: {r.status_code} {r.text[:1000]}")
            if r.status_code >= 300:
                raise RuntimeError(f"OpenAI Responses API failed: {r.status_code} {r.text[:1000]}")
            obj = json.loads(openai_output_text(r.json()))
            break
        except Exception as exc:
            last_error = exc
            if attempt == 4:
                raise RuntimeError(f"OpenAI request failed after retries: {last_error}") from exc
            time.sleep(min(30.0, 2.0 ** attempt))

    values = obj.get("values") or []
    clean: list[str] = []
    seen: set[str] = set()
    doc_norm = quote_norm(target.content_text[:max_doc_chars])
    for value in values:
        s = str(value or "").strip()
        if not s or quote_norm(s) in {"", SENTINEL}:
            continue
        if quote_norm(s) not in doc_norm:
            continue
        if s not in seen:
            seen.add(s)
            clean.append(s)
    return clean


def load_examples(conn, project_id: str, group_id: str) -> dict[str, list[Example]]:
    rows = conn.execute(
        text(
            """
            SELECT ha.annotation_id, ha.document_id, ha.tags, ha.text, ha.exact, ha.prefix, ha.suffix
            FROM hypothesis_annotations ha
            JOIN project_documents pd ON pd.document_id = ha.document_id
            WHERE CAST(pd.project_id AS TEXT) = :project_id
              AND ha.group_id = :group_id
              AND ha.source_type = 'human'
              AND ha.document_id IS NOT NULL
            """
        ),
        {"project_id": project_id, "group_id": group_id},
    ).mappings()
    examples: dict[str, list[Example]] = defaultdict(list)
    for row in rows:
        if has_reject(row["tags"], row["text"]):
            continue
        value = review_value(row["text"], row["exact"])
        if not value:
            continue
        for code in candidate_codes_from_tags(row["tags"]):
            examples[code].append(
                Example(
                    annotation_id=str(row["annotation_id"]),
                    document_id=str(row["document_id"]),
                    code=code,
                    value=value,
                    exact=str(row["exact"] or ""),
                    prefix=str(row["prefix"] or ""),
                    suffix=str(row["suffix"] or ""),
                )
            )
    return examples


def load_targets(conn, project_id: str) -> list[TargetDoc]:
    rows = conn.execute(
        text(
            """
            SELECT d.document_id, d.canonical_url, d.content_text
            FROM documents d
            JOIN project_documents pd ON pd.document_id = d.document_id
            WHERE CAST(pd.project_id AS TEXT) = :project_id
            ORDER BY d.document_id
            """
        ),
        {"project_id": project_id},
    ).mappings()
    return [
        TargetDoc(str(r["document_id"]), normalize_url(r["canonical_url"]) or str(r["canonical_url"]), str(r["content_text"] or ""))
        for r in rows
        if r["document_id"] and r["canonical_url"] and r["content_text"]
    ]


def load_blocked_and_existing(conn, project_id: str, group_id: str) -> tuple[set[tuple[str, str]], set[tuple[str, str]], set[str]]:
    blocked: set[tuple[str, str]] = set()
    human_active: set[tuple[str, str]] = set()
    existing_suggestion_ids: set[str] = set()

    rows = conn.execute(
        text(
            """
            SELECT ha.document_id, ha.tags, ha.text, ha.source_type
            FROM hypothesis_annotations ha
            JOIN project_documents pd ON pd.document_id = ha.document_id
            WHERE CAST(pd.project_id AS TEXT) = :project_id
              AND ha.group_id = :group_id
              AND ha.document_id IS NOT NULL
            """
        ),
        {"project_id": project_id, "group_id": group_id},
    ).mappings()
    for row in rows:
        doc_id = str(row["document_id"])
        tags = normalize_tags(row["tags"])
        refs = {t.split(":", 1)[0]: t.split(":", 1)[1] for t in tags if ":" in t}
        if refs.get("suggestion_id"):
            existing_suggestion_ids.add(refs["suggestion_id"])
        for code in candidate_codes_from_tags(tags):
            if str(row["source_type"]) == "human" and has_reject(tags, row["text"]):
                blocked.add((doc_id, code))
            elif str(row["source_type"]) == "human":
                human_active.add((doc_id, code))

    item_rows = conn.execute(
        text(
            """
            SELECT item_id
            FROM project_review_items
            WHERE CAST(project_id AS TEXT) = :project_id
              AND group_id = :group_id
            """
        ),
        {"project_id": project_id, "group_id": group_id},
    ).mappings()
    for row in item_rows:
        existing_suggestion_ids.add(str(row["item_id"]))

    return blocked, human_active, existing_suggestion_ids


def build_payload(group_id: str, project_id: str, target: TargetDoc, code: str, value: str, selector: dict | None, example_count: int) -> tuple[str, dict]:
    sid = stable_suggestion_id(project_id, target.document_id, code, value)
    tags = [
        "source:model_suggestion",
        "bot:hitl",
        "status:suggested",
        "implicit_accept:true",
        "propagation:hypothesis_review_examples",
        f"example_count:{example_count}",
        "anchored:quote" if selector else "anchored:none",
        f"project_id:{project_id}",
        f"doc_id:{target.document_id}",
        f"field:{code}",
        f"suggestion_id:{sid}",
    ]
    payload = {
        "group": group_id,
        "uri": target.canonical_url,
        "text": "\n".join(
            [
                "[PROPAGATED MODEL SUGGESTION]",
                f"Code: {code}",
                f"Suggested value: {value}",
                "",
                "This suggestion was created from reviewer examples in this project.",
            ]
        ),
        "tags": tags,
        "permissions": {"read": [f"group:{group_id}"]},
    }
    if selector:
        payload["target"] = [{"source": target.canonical_url, "selector": [selector]}]
    return sid, payload


def record_project_review_item(conn, project_id: str, group_id: str, target: TargetDoc, sid: str, code: str, value: str, annotation_id: str | None, anchored: bool) -> None:
    conn.execute(
        text(
            """
            INSERT INTO project_review_items
              (project_id, group_id, item_id, document_id, item_type, code, value, hypothesis_annotation_id, anchored, created_at, updated_at)
            VALUES
              (CAST(:project_id AS UUID), :group_id, :item_id, :document_id, 'model_suggestion', :code, :value, :annotation_id, :anchored, NOW(), NOW())
            ON CONFLICT (project_id, group_id, item_id)
            DO UPDATE SET
              document_id = EXCLUDED.document_id,
              item_type = EXCLUDED.item_type,
              code = EXCLUDED.code,
              value = EXCLUDED.value,
              hypothesis_annotation_id = COALESCE(EXCLUDED.hypothesis_annotation_id, project_review_items.hypothesis_annotation_id),
              anchored = EXCLUDED.anchored,
              updated_at = NOW()
            """
        ),
        {
            "project_id": project_id,
            "group_id": group_id,
            "item_id": sid,
            "document_id": target.document_id,
            "code": code,
            "value": value,
            "annotation_id": annotation_id,
            "anchored": bool(anchored),
        },
    )


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Propagate synced human Hypothesis review tags within one project.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--group", required=True, help="Project review Hypothesis group id")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--hypothesis-token", default=os.getenv("HYPOTHESIS_API_TOKEN", ""))
    parser.add_argument("--openai-api-key", default=os.getenv("OPENAI_API_KEY", ""))
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--min-examples", type=int, default=1)
    parser.add_argument("--examples", type=int, default=5, help="Use up to this many reviewer examples per code")
    parser.add_argument("--max-docs", type=int, default=0)
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--max-values-per-doc-code", type=int, default=5)
    parser.add_argument("--max-doc-chars", type=int, default=45000)
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--execute", action="store_true", help="Create Hypothesis annotations. Default is plan only.")
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("ERROR: DATABASE_URL is required")
    if not args.openai_api_key:
        raise SystemExit("ERROR: OPENAI_API_KEY is required")
    if args.execute and not args.hypothesis_token:
        raise SystemExit("ERROR: HYPOTHESIS_API_TOKEN is required with --execute")

    engine = create_engine(args.database_url, pool_pre_ping=True)
    created = planned = skipped = errors = 0
    started = datetime.now(timezone.utc)

    with engine.begin() as conn:
        examples_by_code = load_examples(conn, args.project_id, args.group)
        targets = load_targets(conn, args.project_id)
        blocked, human_active, existing_sids = load_blocked_and_existing(conn, args.project_id, args.group)

        codes = [
            code for code, examples in sorted(examples_by_code.items())
            if len({ex.document_id for ex in examples}) >= max(1, args.min_examples)
        ]
        if args.max_codes:
            codes = codes[: args.max_codes]
        if args.max_docs:
            targets = targets[: args.max_docs]

        print(f"project_id={args.project_id}")
        print(f"group={args.group}")
        print(f"mode={'EXECUTE' if args.execute else 'PLAN'}")
        print(f"eligible_codes={len(codes)} target_docs={len(targets)} examples_per_code={args.examples}")

        for code in codes:
            examples = examples_by_code[code][: max(1, args.examples)]
            for target in targets:
                if (target.document_id, code) in blocked or (target.document_id, code) in human_active:
                    skipped += 1
                    continue
                try:
                    values = extract_values_with_openai(
                        api_key=args.openai_api_key,
                        model=args.model,
                        code=code,
                        examples=examples,
                        target=target,
                        max_doc_chars=args.max_doc_chars,
                        timeout=180,
                    )[: max(1, args.max_values_per_doc_code)]
                    for value in values:
                        selector = find_unique_text_quote_selector(target.content_text, value)
                        sid, payload = build_payload(args.group, args.project_id, target, code, value, selector, len(examples))
                        if sid in existing_sids:
                            skipped += 1
                            continue
                        planned += 1
                        if args.execute:
                            ann = create_hypothesis_annotation(args.hypothesis_token, payload)
                            record_project_review_item(
                                conn,
                                args.project_id,
                                args.group,
                                target,
                                sid,
                                code,
                                value,
                                (ann or {}).get("id"),
                                bool(selector),
                            )
                            existing_sids.add(sid)
                            created += 1
                        if planned % 25 == 0:
                            print(f"planned={planned} created={created} skipped={skipped} errors={errors}")
                        if args.sleep > 0 and args.execute:
                            time.sleep(args.sleep)
                except Exception as exc:
                    errors += 1
                    print(f"ERROR code={code} document_id={target.document_id}: {exc}")

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"done planned={planned} created={created} skipped={skipped} errors={errors} elapsed_s={elapsed:.1f}")
    if not args.execute:
        print("PLAN ONLY: rerun with --execute to create Hypothesis suggestions.")


if __name__ == "__main__":
    main()
