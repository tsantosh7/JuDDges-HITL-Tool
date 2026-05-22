#!/usr/bin/env python3
import os
import re
import json
import argparse
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple
from json import JSONDecoder

import requests

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from field_guide import build_field_guide_text

SENTINEL = "data not available"


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def load_v1_fields(schema_codes_path: str) -> List[str]:
    with open(schema_codes_path, "r", encoding="utf-8") as f:
        obj = json.load(f)

    if isinstance(obj, dict) and "v1_codes" in obj and isinstance(obj["v1_codes"], list):
        fields = obj["v1_codes"]
    elif isinstance(obj, list):
        fields = obj
    else:
        raise ValueError("Unexpected schema format. Expected {'v1_codes':[...]} or a list.")

    if not fields or not all(isinstance(x, str) and x.strip() for x in fields):
        raise ValueError("Schema field list is empty or contains non-strings.")

    return fields


def build_output1_schema(schema_codes_path: str) -> Dict[str, Any]:
    fields = load_v1_fields(schema_codes_path)
    props = {
        field: {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        }
        for field in fields
    }
    return {
        "name": "output_1",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": props,
            "required": fields,
            "additionalProperties": False,
        },
        "keys": fields,
    }


def extract_from_finetune_messages(row: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    msgs = row.get("messages")
    if not isinstance(msgs, list):
        return None, None

    user_text = None
    for m in reversed(msgs):
        if m.get("role") == "user":
            user_text = m.get("content")
            break

    gold_obj = None
    for m in reversed(msgs):
        if m.get("role") == "assistant":
            content = m.get("content")
            if isinstance(content, dict):
                gold_obj = content
            elif isinstance(content, str):
                try:
                    gold_obj = json.loads(content)
                except Exception:
                    gold_obj = None
            break

    return user_text, gold_obj


def extract_doc_text(row: Dict[str, Any]) -> str:
    if "input" in row and isinstance(row["input"], str) and row["input"].strip():
        return row["input"]

    user_text, _ = extract_from_finetune_messages(row)
    if isinstance(user_text, str) and user_text.strip():
        return user_text

    for k in ("content_text", "document", "content", "body", "body_txt"):
        if k in row and isinstance(row[k], str) and row[k].strip():
            return row[k]

    raise KeyError("Could not find document text. Expected 'input' or finetune 'messages' or common text keys.")


def extract_gold_output1(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if "output_1" in row:
        gold = row["output_1"]
        if isinstance(gold, dict):
            return gold
        if isinstance(gold, str):
            try:
                return json.loads(gold)
            except Exception:
                return None
    _, gold_obj = extract_from_finetune_messages(row)
    return gold_obj


def get_doc_id(row: Dict[str, Any], fallback: str) -> str:
    return str(row.get("document_id") or row.get("id") or row.get("doc_id") or fallback)


def parse_first_json_object(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    dec = JSONDecoder()

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    try:
        obj, _idx = dec.raw_decode(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    return {"__raw__": text}


def response_output_text(data: Dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    parts: List[str] = []
    for item in data.get("output") or []:
        for content in item.get("content") or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "\n".join(parts)


def create_response_text(client, *, model: str, messages: List[Dict[str, str]], schema_cfg: Dict[str, Any]) -> str:
    text_format = {
        "format": {
            "type": "json_schema",
            "name": schema_cfg["name"],
            "strict": schema_cfg["strict"],
            "schema": schema_cfg["schema"],
        }
    }
    payload = {
        "model": model,
        "input": messages,
        "text": text_format,
    }
    last_error: Exception | None = None

    for attempt in range(5):
        try:
            if client is not None:
                resp = client.responses.create(model=model, input=messages, text=text_format)
                return resp.output_text

            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is not set and the openai Python package is not installed")

            r = requests.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=180,
            )
            if r.status_code in {408, 409, 429} or r.status_code >= 500:
                raise RuntimeError(f"OpenAI transient failure: {r.status_code} {r.text[:1000]}")
            if r.status_code >= 300:
                raise RuntimeError(f"OpenAI Responses API failed: {r.status_code} {r.text[:1000]}")
            return response_output_text(r.json())
        except Exception as exc:
            last_error = exc
            if attempt == 4:
                break
            time.sleep(min(30.0, 2.0 ** attempt))

    raise RuntimeError(f"OpenAI request failed after retries: {last_error}")


def _norm_for_match(s: str) -> str:
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def enforce_grounding(pred: Dict[str, Any], doc_text: str, sentinel: str = SENTINEL) -> Dict[str, Any]:
    """
    Keep only values that appear verbatim (case-insensitive, whitespace-normalized) in doc_text.
    If nothing survives for a field, set it to [sentinel].
    """
    doc_norm = _norm_for_match(doc_text)
    out: Dict[str, Any] = {}

    for field, values in pred.items():
        if not isinstance(values, list) or not values:
            out[field] = [sentinel]
            continue

        kept: List[str] = []
        for v in values:
            if not isinstance(v, str):
                continue
            v_clean = v.strip()
            if not v_clean:
                continue

            if v_clean.lower() == sentinel:
                kept.append(sentinel)
                continue

            if _norm_for_match(v_clean) in doc_norm:
                kept.append(v_clean)

        out[field] = kept if kept else [sentinel]

    return out


def apply_field_sanity_rules(pred: Dict[str, Any]) -> Dict[str, Any]:
    """
    OUTPUT_1 phase: do NOT apply semantic pruning.
    Humans may label 'he/she', procedural phrases, etc.
    Keep only grounding constraints (handled elsewhere).
    """
    return pred


def normalize_pred_keys(pred: Dict[str, Any], expected_fields: List[str]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for f in expected_fields:
        v = pred.get(f)
        if isinstance(v, list) and len(v) > 0:
            clean[f] = v
        else:
            clean[f] = [SENTINEL]
    return clean


def predict_one(
    *,
    client,
    row: Dict[str, Any],
    row_index: int,
    model: str,
    system_text: str,
    fewshot_msgs: List[Dict[str, str]],
    schema_cfg: Dict[str, Any],
    expected_fields: List[str],
    expected_set: set[str],
    max_doc_chars: int,
) -> tuple[Dict[str, Any], bool]:
    doc_id = get_doc_id(row, fallback=f"row_{row_index}")

    doc_text = extract_doc_text(row)
    if len(doc_text) > max_doc_chars:
        doc_text = doc_text[:max_doc_chars]

    messages: List[Dict[str, str]] = [{"role": "system", "content": system_text}]
    messages.extend(fewshot_msgs)
    messages.append({"role": "user", "content": "Extract output_1 for the document below.\n\nDOCUMENT:\n" + doc_text})

    pred_obj = parse_first_json_object(
        create_response_text(
            client,
            model=model,
            messages=messages,
            schema_cfg=schema_cfg,
        )
    )

    got_keys = set(pred_obj.keys()) if isinstance(pred_obj, dict) else set()
    if got_keys != expected_set:
        return {
            "_type": "prediction",
            "run_id": "",
            "document_id": doc_id,
            "prediction_output_1": pred_obj,
            "__schema_mismatch__": {
                "missing": sorted(list(expected_set - got_keys)),
                "extra": sorted(list(got_keys - expected_set)),
            },
        }, False

    pred_obj = enforce_grounding(pred_obj, doc_text, SENTINEL)
    pred_obj = apply_field_sanity_rules(pred_obj)
    pred_obj = normalize_pred_keys(pred_obj, expected_fields)

    return {
        "_type": "prediction",
        "run_id": "",
        "document_id": doc_id,
        "prediction_output_1": pred_obj,
    }, True


def build_system_instructions() -> str:
    field_guide = build_field_guide_text()
    return (
        "You are an information extraction system.\n"
        "Return output_1 as JSON ONLY, matching the provided schema.\n"
        "For each field: return a list of 1+ strings.\n"
        f"If the document does not contain explicit evidence for a field, return ['{SENTINEL}'].\n"
        "\n"
        "CRITICAL GROUNDING RULES:\n"
        "- For any value other than 'data not available', you MUST copy an exact substring from the DOCUMENT.\n"
        "- Do NOT paraphrase, infer, generalize, or add references not present in the DOCUMENT.\n"
        "- When in doubt, return ['data not available'].\n"
        "\n"
        f"{field_guide}\n"
    )


def format_fewshot_messages(train_rows: List[Dict[str, Any]], k: int, seed: int, max_example_chars: int) -> List[Dict[str, str]]:
    if k <= 0:
        return []

    rng = random.Random(seed)
    chosen = train_rows if k >= len(train_rows) else rng.sample(train_rows, k)

    msgs: List[Dict[str, str]] = []
    for ex in chosen:
        doc_text = extract_doc_text(ex)
        if len(doc_text) > max_example_chars:
            doc_text = doc_text[:max_example_chars]

        gold_obj = extract_gold_output1(ex)
        if not isinstance(gold_obj, dict):
            raise ValueError("Few-shot training row missing gold output_1")

        msgs.append({"role": "user", "content": "Extract output_1 for the document below.\n\nDOCUMENT:\n" + doc_text})
        msgs.append({"role": "assistant", "content": json.dumps(gold_obj, ensure_ascii=False)})

    return msgs


def read_existing_predictions(out_path: str) -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(out_path):
        return {}

    seen: Dict[str, Dict[str, Any]] = {}
    with open(out_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("_type") != "prediction":
                continue
            doc_id = obj.get("document_id")
            if doc_id:
                seen[str(doc_id)] = obj
    return seen


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", default="data/train.finetune.jsonl")
    ap.add_argument("--test", default="data/test.finetune.jsonl")
    ap.add_argument("--schema", default="data/schema_v1_codes.json")
    ap.add_argument("--k", type=int, default=0)
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--runs_dir", default="runs")
    ap.add_argument("--max_doc_chars", type=int, default=45000)
    ap.add_argument("--max_example_chars", type=int, default=9000)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=1, help="Parallel OpenAI requests. Keep low to respect rate limits.")
    args = ap.parse_args()

    client = OpenAI() if OpenAI is not None else None

    train_rows = load_jsonl(args.train)
    test_rows = load_jsonl(args.test)

    if args.limit is not None:
        test_rows = test_rows[: args.limit]

    schema_cfg = build_output1_schema(args.schema)
    expected_fields = schema_cfg["keys"]
    expected_set = set(expected_fields)

    system_text = build_system_instructions()
    fewshot_msgs = format_fewshot_messages(train_rows, args.k, args.seed, args.max_example_chars)

    run_slug = f"k{args.k}_{args.model}_seed{args.seed}"
    run_dir = os.path.join(args.runs_dir, run_slug)
    ensure_dir(run_dir)

    out_path = os.path.join(run_dir, "predictions.jsonl")

    existing = read_existing_predictions(out_path) if args.resume else {}
    if os.path.exists(out_path) and not args.resume:
        raise RuntimeError(f"{out_path} already exists. Use --resume or delete it.")

    run_id_path = os.path.join(run_dir, "run_id.txt")
    if os.path.exists(run_id_path):
        with open(run_id_path, "r", encoding="utf-8") as f:
            run_id = f.read().strip()
    else:
        run_id = str(uuid.uuid4())
        with open(run_id_path, "w", encoding="utf-8") as f:
            f.write(run_id)

    is_new_file = (not os.path.exists(out_path)) or (os.path.getsize(out_path) == 0)
    if is_new_file:
        with open(out_path, "a", encoding="utf-8") as out_f:
            header = {
                "_type": "run_metadata",
                "run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "model": args.model,
                "k_shot": args.k,
                "seed": args.seed,
                "schema_file": os.path.basename(args.schema),
                "train_file": os.path.basename(args.train),
                "test_file": os.path.basename(args.test),
                "sentinel": SENTINEL,
                "grounding": "verbatim_substring_required_case_insensitive_ws_normalized",
                "field_guide": "enabled",
            }
            out_f.write(json.dumps(header, ensure_ascii=False) + "\n")

    total = len(test_rows)
    written = 0
    skipped = 0

    with open(out_path, "a", encoding="utf-8") as out_f:
        pending: list[tuple[int, Dict[str, Any]]] = []
        for i, row in enumerate(test_rows):
            doc_id = get_doc_id(row, fallback=f"row_{i}")
            if args.resume and doc_id in existing:
                skipped += 1
                continue
            pending.append((i, row))

        def write_result(out_row: Dict[str, Any], ok: bool) -> None:
            nonlocal written
            out_row["run_id"] = run_id
            out_f.write(json.dumps(out_row, ensure_ascii=False) + "\n")
            out_f.flush()
            if ok:
                written += 1
            if written and (written % 10) == 0:
                print(f"Progress: wrote {written} (skipped {skipped}) / {total}")

        def submit_kwargs(i: int, row: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "client": client,
                "row": row,
                "row_index": i,
                "model": args.model,
                "system_text": system_text,
                "fewshot_msgs": fewshot_msgs,
                "schema_cfg": schema_cfg,
                "expected_fields": expected_fields,
                "expected_set": expected_set,
                "max_doc_chars": args.max_doc_chars,
            }

        if max(1, args.workers) == 1:
            for i, row in pending:
                out_row, ok = predict_one(**submit_kwargs(i, row))
                write_result(out_row, ok)
        else:
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                futures = [pool.submit(predict_one, **submit_kwargs(i, row)) for i, row in pending]
                for future in as_completed(futures):
                    out_row, ok = future.result()
                    write_result(out_row, ok)

    print(f"Done. Wrote {written}, skipped {skipped}. Saved: {out_path}")


if __name__ == "__main__":
    main()
