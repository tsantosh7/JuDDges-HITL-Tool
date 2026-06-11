#!/usr/bin/env python3
import argparse
import json
import random
from pathlib import Path
from typing import Dict, Any, List, Set

SENTINEL = "data not available"


# Minimal descriptions. You can expand later; examples will be real from train set.
# IMPORTANT: OffSex/VicSex updated to allow pronouns because Output_1 is verbatim.
BASE_DESCRIPTIONS: Dict[str, str] = {
    "ConvCourtName": "Name(s) of the court where the defendant was convicted or pleaded guilty (verbatim).",
    "ConvictPleaDate": "Date(s) on which the defendant was convicted or pleaded guilty (verbatim).",
    "ConvictOffence": "Offence(s) of which the defendant was convicted (verbatim).",
    "AcquitOffence": "Offence(s) of which the defendant was acquitted (verbatim).",
    "ConfessPleadGuilty": "Did the defendant confess or plead guilty? (verbatim: e.g., 'pleaded guilty', 'pleaded not guilty').",
    "PleaPoint": "Stage at which the plea was entered (verbatim).",
    "RemandDecision": "Remand decision post-conviction/remand stage (verbatim).",
    "RemandCustodyTime": "Duration of any remand in custody (verbatim).",
    "SentCourtName": "Name(s) of the court where the defendant was sentenced (verbatim).",
    "Sentence": "Original Sentence(s) Received.",
    "SentServe": "How sentences run (verbatim: concurrent/consecutive/combination or exact phrasing).",
    "WhatAncillary": "Ancillary orders applied by the court (verbatim).",

    # Key change:
    "OffSex": "Verbatim gender indicator(s) for offender(s) as written in the document (e.g., 'he', 'she', 'male', 'female', etc.).",
    "VicSex": "Verbatim gender indicator(s) for victim(s) as written in the document (e.g., 'he', 'she', 'male', 'female', etc.).",

    "OffAgeOffence": "Age of defendant at offence (verbatim).",
    "OffJobOffence": "Employment/occupation at offence (verbatim).",
    "OffHomeOffence": "Accommodation status at offence (verbatim).",
    "OffMentalOffence": "Mental health / learning difficulties at offence (verbatim).",
    "OffIntoxOffence": "Intoxication status at offence (verbatim).",
    "OffVicRelation": "Relationship defendant→victim (verbatim).",
    "VictimType": "Type of victim (verbatim).",
    "VicNum": "Number of victims or ratio (verbatim).",
    "VicAgeOffence": "Age of victim(s) at offence (verbatim).",
    "VicJobOffence": "Employment/occupation of victim(s) at offence (verbatim).",
    "VicHomeOffence": "Accommodation status of victim(s) at offence (verbatim).",
    "VicMentalOffence": "Mental health / learning difficulties for victim(s) (verbatim).",
    "VicIntoxOffence": "Victim intoxication status at offence (verbatim).",
    "ProsEvidTypeTrial": "Evidence types by prosecution at trial (verbatim).",
    "DefEvidTypeTrial": "Evidence types by defence at trial (verbatim).",
    "PreSentReport": "Pre-sentence report content / risk wording (verbatim).",
    "AggFactSent": "Aggravating factors at sentencing (verbatim phrases).",
    "MitFactSent": "Mitigating factors at sentencing (verbatim phrases).",
    "VicImpactStatement": "Victim impact statement provided? (verbatim).",
    "Appellant": "Who brings the appeal (verbatim).",
    "CoDefAccNum": "Number of co-defendants/co-accused (verbatim).",
    "AppealAgainst": "Ground(s) for appeal (verbatim).",
    "AppealGround": "Specific legal grounds of appeal (verbatim).",
    "SentGuideWhich": "Sentencing guidelines/statutes cited (verbatim).",
    "AppealOutcome": "Outcome of the appeal (verbatim).",
    "ReasonQuashConv": "Reasons for quashing conviction (verbatim).",
    "ReasonSentExcessNotLenient": "Reasons why sentence was unduly excessive / not lenient (verbatim).",
    "ReasonSentLenientNotExcess": "Reasons why sentence was unduly lenient / not excessive (verbatim).",
    "ReasonDismiss": "Reasons for dismissal (verbatim).",
}


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def extract_assistant_json(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    train.finetune.jsonl format: {"messages":[..., {"role":"assistant","content":"{...json...}"}]}
    """
    msgs = row.get("messages", [])
    if not isinstance(msgs, list):
        return {}
    # last assistant message is usually the label
    for m in reversed(msgs):
        if m.get("role") == "assistant" and isinstance(m.get("content"), str):
            s = m["content"].strip()
            try:
                return json.loads(s)
            except Exception:
                return {}
    return {}


def ensure_list_of_strings(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        out = []
        for x in v:
            if x is None:
                continue
            if not isinstance(x, str):
                x = str(x)
            x = x.strip()
            if x:
                out.append(x)
        return out
    if isinstance(v, str):
        v = v.strip()
        return [v] if v else []
    return [str(v).strip()]


def python_repr(obj: Any) -> str:
    # stable-ish representation for python file
    return repr(obj)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", default="data/train.finetune.jsonl")
    ap.add_argument("--schema", default="data/schema_v1_codes.json")
    ap.add_argument("--out", default="fields.py")
    ap.add_argument("--k", type=int, default=20, help="max unique examples per field")
    ap.add_argument("--seed", type=int, default=13)
    args = ap.parse_args()

    random.seed(args.seed)

    train_path = Path(args.train)
    schema_path = Path(args.schema)
    out_path = Path(args.out)

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    fields = schema.get("v1_codes", [])
    if not fields:
        raise SystemExit("schema_v1_codes.json missing v1_codes")

    # collect unique values per field
    uniques: Dict[str, Set[str]] = {f: set() for f in fields}

    for row in load_jsonl(train_path):
        y = extract_assistant_json(row)
        if not isinstance(y, dict):
            continue
        for f in fields:
            vals = ensure_list_of_strings(y.get(f))
            for s in vals:
                if not s:
                    continue
                if s.strip().lower() == SENTINEL:
                    continue
                uniques[f].add(s)

    # sample examples
    examples: Dict[str, List[str]] = {}
    for f in fields:
        items = sorted(list(uniques[f]))
        if not items:
            examples[f] = [SENTINEL]
            continue
        if len(items) > args.k:
            examples[f] = random.sample(items, args.k)
        else:
            examples[f] = items

    # write fields.py
    lines = []
    lines.append("# Auto-generated from training data\n")
    lines.append(f"SENTINEL = {python_repr(SENTINEL)}\n\n")
    lines.append("FIELDS = {\n")
    for f in fields:
        desc = BASE_DESCRIPTIONS.get(f, "Verbatim extraction field.")
        spec = {
            "type": "list",
            "items": {"type": "string"},
            "description": desc,
            "example": examples.get(f, [SENTINEL]),
        }
        lines.append(f"  {python_repr(f)}: {python_repr(spec)},\n")
    lines.append("}\n")

    out_path.write_text("".join(lines), encoding="utf-8")
    print(f"Wrote {out_path.resolve()}")


if __name__ == "__main__":
    main()
