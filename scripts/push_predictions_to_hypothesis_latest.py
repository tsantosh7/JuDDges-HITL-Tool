import os
import json
import argparse
import hashlib
import re
import time
import sys
from pathlib import Path

from typing import Dict, List, Optional, Tuple, Iterable, Set

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


HYPOTHESIS_API_BASE = "https://api.hypothes.is/api"




# -----------------------------
# HTTP session with retries
# -----------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=8,
        connect=8,
        read=8,
        status=8,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "DELETE"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def hyp_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.hypothesis.v1+json",
        "Content-Type": "application/json;charset=utf-8",
    }


# -----------------------------
# Hypothesis API helpers
# -----------------------------
def hyp_get_profile(session: requests.Session, token: str) -> dict:
    r = session.get(f"{HYPOTHESIS_API_BASE}/profile", headers=hyp_headers(token), timeout=60)
    r.raise_for_status()
    return r.json()


def hyp_create_annotation(session: requests.Session, token: str, payload: dict) -> dict:
    r = session.post(f"{HYPOTHESIS_API_BASE}/annotations", headers=hyp_headers(token), json=payload, timeout=60)
    r.raise_for_status()
    return r.json()


def hyp_delete_annotation(session: requests.Session, token: str, ann_id: str) -> None:
    r = session.delete(f"{HYPOTHESIS_API_BASE}/annotations/{ann_id}", headers=hyp_headers(token), timeout=60)
    # tolerate already deleted/not found
    if r.status_code not in (200, 204, 404):
        r.raise_for_status()


def hyp_search_rows(
    session: requests.Session,
    token: str,
    *,
    group: str,
    limit: int = 200,
    sort: str = "updated",
    order: str = "desc",
    tags: Optional[List[str]] = None,
    tag: Optional[str] = None,
    uri: Optional[str] = None,
    user: Optional[str] = None,
    references: Optional[str] = None,
) -> List[dict]:
    """
    One page search. For our usage (per-doc filtering), one page is usually enough.
    """
    params: Dict[str, object] = {"group": group, "limit": int(limit), "sort": sort, "order": order}
    if tag:
        params["tag"] = tag
    if tags:
        params["tag"] = tags  # requests sends repeated tag params
    if uri:
        params["uri"] = uri
    if user:
        params["user"] = user
    if references:
        params["references"] = references

    r = session.get(f"{HYPOTHESIS_API_BASE}/search", params=params, headers=hyp_headers(token), timeout=60)
    r.raise_for_status()
    return r.json().get("rows", []) or []


# -----------------------------
# JSONL loading
# -----------------------------
def load_normalised_index(path: str) -> Dict[str, dict]:
    idx: Dict[str, dict] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            doc_id = obj.get("document_id")
            if doc_id:
                idx[doc_id] = obj
    return idx


def iter_predictions(path: str) -> Iterable[dict]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("_type") == "prediction":
                yield obj


def find_run_metadata(path: str) -> Optional[dict]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("_type") == "run_metadata":
                return obj
    return None


# -----------------------------
# Anchoring (strict hybrid)
# -----------------------------
_ws = re.compile(r"\s+")


def norm(s: str) -> str:
    return _ws.sub(" ", (s or "")).strip().lower()


def find_unique_span(
    content_text: str,
    value: str,
    *,
    window: int = 50,
    min_len: int = 5,
) -> Optional[Tuple[str, str, str]]:
    """
    Returns (exact, prefix, suffix) from ORIGINAL content_text only if:
      - value appears exactly once in normalized content
      - and we find exactly one whitespace-flexible match in original text
    Strict to avoid drift.
    """
    if not content_text or not value:
        return None

    v_raw = value.strip()
    if len(norm(v_raw)) < min_len:
        return None

    ct = content_text
    ct_n = norm(ct)
    v_n = norm(v_raw)

    # Unique occurrence in normalized text
    start = 0
    hits = 0
    while True:
        i = ct_n.find(v_n, start)
        if i == -1:
            break
        hits += 1
        if hits > 1:
            return None
        start = i + 1
    if hits != 1:
        return None

    # Match in original with flexible whitespace
    parts = re.split(r"\s+", v_raw)
    pattern = r"\s+".join(re.escape(p) for p in parts if p)
    rx = re.compile(pattern, re.IGNORECASE)

    matches = list(rx.finditer(ct))
    if len(matches) != 1:
        return None

    m = matches[0]
    exact = ct[m.start() : m.end()]
    prefix = ct[max(0, m.start() - window) : m.start()]
    suffix = ct[m.end() : min(len(ct), m.end() + window)]
    return exact, prefix, suffix


# -----------------------------
# Tags / ids
# -----------------------------
def stable_pred_id(run_id: str, doc_id: str, field: str, value: str) -> str:
    raw = f"{run_id}|{doc_id}|{field}|{value}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()


def tag_doc(doc_id: str) -> str:
    return f"doc:{doc_id}"


def tag_run(run_id: str) -> str:
    return f"run:{run_id}"


def tag_field(field: str) -> str:
    return f"field:{field}"


def model_text(field: str, value: str, *, anchored: bool) -> str:
    if anchored:
        return f"[MODEL] {field} = {value}"
    return f"[MODEL] {field} = {value}\n\n(Unable to auto-highlight; please review.)"


# -----------------------------
# Prefetch dedupe set per doc
# -----------------------------
def collect_existing_pred_ids_for_doc(
    session: requests.Session,
    token: str,
    *,
    group: str,
    doc_id: str,
    run_id: str,
) -> Set[str]:
    """
    Fetch existing bot/model annotations for THIS doc+run and collect pred_id:* tags.
    This avoids per-value /search calls (big performance win).
    """
    rows = hyp_search_rows(
        session,
        token,
        group=group,
        tags=[tag_doc(doc_id), "source:model", tag_run(run_id)],
        limit=200,
        sort="updated",
        order="desc",
    )
    out: Set[str] = set()
    for a in rows:
        for t in (a.get("tags") or []):
            if isinstance(t, str) and t.startswith("pred_id:"):
                out.add(t)
    return out


# -----------------------------
# Cleanup old runs (safe P1)
# -----------------------------
def has_human_replies(
    session: requests.Session,
    token: str,
    *,
    group: str,
    parent_id: str,
    bot_userid: str,
) -> bool:
    rows = hyp_search_rows(
        session,
        token,
        group=group,
        references=parent_id,
        limit=200,
        sort="updated",
        order="desc",
    )
    for r in rows:
        u = r.get("user")
        if u and u != bot_userid:
            return True
    return False


def cleanup_old_bot_annotations_for_doc(
    session: requests.Session,
    token: str,
    *,
    group: str,
    doc_id: str,
    latest_run_id: str,
    bot_userid: str,
    sleep_s: float,
    dry_run: bool,
) -> int:
    """
    Deletes bot-owned annotations for this doc that are NOT latest run,
    but ONLY if they have NO human replies (Policy P1).
    """
    deleted = 0
    rows = hyp_search_rows(
        session,
        token,
        group=group,
        tags=[tag_doc(doc_id), "source:model"],
        limit=200,
        sort="updated",
        order="desc",
    )

    for ann in rows:
        if ann.get("user") != bot_userid:
            continue

        ann_id = ann.get("id")
        if not ann_id:
            continue

        tset = set(ann.get("tags") or [])
        if tag_run(latest_run_id) in tset:
            continue

        if has_human_replies(session, token, group=group, parent_id=ann_id, bot_userid=bot_userid):
            continue

        if dry_run:
            print(f"[dry-run] would delete old bot annotation {ann_id} (doc={doc_id})")
        else:
            hyp_delete_annotation(session, token, ann_id)
        deleted += 1

        if sleep_s:
            time.sleep(sleep_s)

    return deleted


# -----------------------------
# Main
# -----------------------------
# def main():
#     ap = argparse.ArgumentParser()
#     ap.add_argument("--token", default=os.getenv("HYPOTHESIS_API_TOKEN", ""), help="Hypothesis API token")
#     ap.add_argument("--group", required=True, help="Hypothesis group id")
#     ap.add_argument("--predictions", required=True, help="Predictions JSONL (_type=prediction + optional run_metadata)")
#     ap.add_argument("--normalised", required=True, help="Normalised JSONL with canonical_url + content_text")
#     ap.add_argument("--run-id", default="", help="Override run_id (otherwise from run_metadata or first prediction)")
#     ap.add_argument("--max-per-doc", type=int, default=500, help="Safety cap on annotations per document")
#     ap.add_argument("--sleep", type=float, default=0.05, help="Sleep between API calls")
#     ap.add_argument("--dry-run", action="store_true", default=False)
#     args = ap.parse_args()
#
#     if not args.token.strip():
#         raise SystemExit("ERROR: provide --token or set HYPOTHESIS_API_TOKEN")
#
#     session = make_session()
#
#     # Determine run_id
#     run_id = args.run_id.strip()
#     if not run_id:
#         md = find_run_metadata(args.predictions)
#         if md and md.get("run_id"):
#             run_id = md["run_id"]
#     if not run_id:
#         for p in iter_predictions(args.predictions):
#             if p.get("run_id"):
#                 run_id = p["run_id"]
#                 break
#     if not run_id:
#         raise SystemExit("ERROR: Could not determine run_id. Pass --run-id explicitly.")
#
#     norm_idx = load_normalised_index(args.normalised)
#
#     total_items = count_work(args.predictions)
#     done_items = 0
#     print(f"Planned items (non-empty predictions): {total_items}")
#
#
#     # Identify bot userid (only needed for cleanup).
#     bot_userid = None
#     if not args.dry_run:
#         profile = hyp_get_profile(session, args.token)
#         bot_userid = profile.get("userid") or profile.get("user") or profile.get("username")
#
#         if not bot_userid:
#             print("DEBUG /profile keys:", sorted(profile.keys()))
#             print("DEBUG /profile:", json.dumps(profile, indent=2)[:2000])
#             raise SystemExit("ERROR: could not determine bot userid from /profile")
#
#         # normalize to acct: form if needed
#         if isinstance(bot_userid, str) and "@hypothes.is" in bot_userid and not bot_userid.startswith("acct:"):
#             bot_userid = f"acct:{bot_userid}"
#
#     created = 0
#     skipped = 0
#     deduped = 0
#     anchored = 0
#     unanchored = 0
#     cleaned_total = 0
#
#     # In dry-run we still want to dedupe locally within this run to avoid repeated prints.
#     global_seen_pid: Set[str] = set()
#
#     for pred_obj in iter_predictions(args.predictions):
#         doc_id = pred_obj.get("document_id")
#         if not doc_id:
#             skipped += 1
#             continue
#
#         nrow = norm_idx.get(doc_id) or {}
#         uri = nrow.get("canonical_url")
#         content_text = nrow.get("content_text") or ""
#
#         if not uri:
#             skipped += 1
#             continue
#
#         # Prefetch existing pred_id tags ONCE per doc (only in real run)
#         existing_pid_tags: Set[str] = set()
#         if not args.dry_run:
#             existing_pid_tags = collect_existing_pred_ids_for_doc(
#                 session, args.token, group=args.group, doc_id=doc_id, run_id=run_id
#             )
#
#         pred_fields = pred_obj.get("prediction_output_1") or {}
#         per_doc_count = 0
#
#         for field, values in pred_fields.items():
#             if not isinstance(values, list):
#                 values = [values]
#
#             for value in values:
#                 value = (value or "").strip()
#                 if not value or value == "data not available":
#                     continue
#
#                 per_doc_count += 1
#                 if per_doc_count > args.max_per_doc:
#                     break
#
#                 pid = stable_pred_id(run_id, doc_id, field, value)
#                 pid_tag = f"pred_id:{pid}"
#
#                 # Local dedupe in the same script run
#                 if pid_tag in global_seen_pid:
#                     deduped += 1
#                     continue
#                 global_seen_pid.add(pid_tag)
#
#                 # Dedupe against existing Hypothesis annotations for doc+run (prefetched)
#                 if not args.dry_run and pid_tag in existing_pid_tags:
#                     deduped += 1
#                     continue
#
#                 span = find_unique_span(content_text, value)
#                 is_anchored = span is not None
#
#                 tags = [
#                     "source:model",
#                     "bot:hitl",
#                     tag_run(run_id),
#                     tag_doc(doc_id),
#                     tag_field(field),
#                     pid_tag,
#                     "kind:prediction",
#                     "anchored:quote" if is_anchored else "anchored:none",
#                 ]
#
#                 payload = {
#                     "group": args.group,
#                     "uri": uri,
#                     "text": model_text(field, value, anchored=is_anchored),
#                     "tags": tags,
#                     "permissions": {"read": [f"group:{args.group}"]},
#                 }
#
#                 if is_anchored:
#                     exact, prefix, suffix = span  # type: ignore
#                     payload["target"] = [
#                         {
#                             "source": uri,
#                             "selector": [
#                                 {
#                                     "type": "TextQuoteSelector",
#                                     "exact": exact,
#                                     "prefix": prefix,
#                                     "suffix": suffix,
#                                 }
#                             ],
#                         }
#                     ]
#
#                 if args.dry_run:
#                     print(json.dumps(payload, indent=2))
#                     created += 1
#                     anchored += int(is_anchored)
#                     unanchored += int(not is_anchored)
#                 else:
#                     hyp_create_annotation(session, args.token, payload)
#                     created += 1
#                     anchored += int(is_anchored)
#                     unanchored += int(not is_anchored)
#
#                     # Keep prefetch set up to date so we don't re-post same pid in same doc
#                     existing_pid_tags.add(pid_tag)
#
#                 if args.sleep:
#                     time.sleep(args.sleep)
#
#         # Cleanup old runs for this doc (only in real run)
#         if not args.dry_run and bot_userid:
#             cleaned = cleanup_old_bot_annotations_for_doc(
#                 session,
#                 args.token,
#                 group=args.group,
#                 doc_id=doc_id,
#                 latest_run_id=run_id,
#                 bot_userid=bot_userid,
#                 sleep_s=args.sleep,
#                 dry_run=args.dry_run,
#             )
#             cleaned_total += cleaned
#
#     print("\n✅ DONE")
#     print(f"run_id={run_id}")
#     print(f"created={created} deduped={deduped} skipped={skipped}")
#     print(f"anchored={anchored} unanchored={unanchored}")
#     print(f"cleaned_old_bot_annotations={cleaned_total}")
#     if bot_userid:
#         print(f"bot_userid={bot_userid}")

def progress(i: int, total: int, *, prefix: str = "", width: int = 30) -> None:
    if total <= 0:
        return
    if i < 0:
        i = 0
    if i > total:
        i = total
    frac = i / total
    filled = int(width * frac)
    bar = "#" * filled + "-" * (width - filled)
    msg = f"\r{prefix}[{bar}] {i}/{total} ({frac*100:5.1f}%)"
    sys.stdout.write(msg)
    sys.stdout.flush()
    if i >= total:
        sys.stdout.write("\n")


def count_work(pred_path: str) -> int:
    """
    Count non-empty predicted values (ignores "data not available") to estimate progress.
    Fast local scan.
    """
    total = 0
    for pred_obj in iter_predictions(pred_path):
        pred_fields = pred_obj.get("prediction_output_1") or {}
        for _, values in pred_fields.items():
            if isinstance(values, list):
                for v in values:
                    s = (v or "").strip()
                    if s and s != "data not available":
                        total += 1
            else:
                s = (values or "")
                s = str(s).strip()
                if s and s != "data not available":
                    total += 1
    return total


def load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path: str, state: dict) -> None:
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)

def collect_existing_pred_ids_for_doc(
    session: requests.Session,
    token: str,
    *,
    group: str,
    doc_id: str,
    run_id: str,
) -> Set[str]:
    """
    Fetch existing bot/model annotations for THIS doc and collect pred_id:* tags.
    NOTE: we intentionally DO NOT filter by run tag, so we dedupe across runs too.
    """
    rows = hyp_search_rows(
        session,
        token,
        group=group,
        tags=[tag_doc(doc_id), "source:model"],
        limit=200,
        sort="updated",
        order="desc",
    )
    out: Set[str] = set()
    for a in rows:
        for t in (a.get("tags") or []):
            if isinstance(t, str) and t.startswith("pred_id:"):
                out.add(t)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.getenv("HYPOTHESIS_API_TOKEN", ""), help="Hypothesis API token")
    ap.add_argument("--group", required=True, help="Hypothesis group id")
    ap.add_argument("--predictions", required=True, help="Predictions JSONL (_type=prediction + optional run_metadata)")
    ap.add_argument("--normalised", required=True, help="Normalised JSONL with canonical_url + content_text")
    ap.add_argument("--run-id", default="", help="Override run_id (otherwise from run_metadata or first prediction)")
    ap.add_argument("--max-per-doc", type=int, default=500, help="Safety cap on annotations per document")
    ap.add_argument("--sleep", type=float, default=0.05, help="Sleep between API calls")
    ap.add_argument("--dry-run", action="store_true", default=False, help="Print payloads instead of uploading")
    ap.add_argument("--plan", action="store_true", default=False, help="Plan only: count would-create vs dedupe (no writes, no payload prints)")
    ap.add_argument("--no-cleanup", action="store_true", default=False, help="Disable cleanup of older bot annotations")
    ap.add_argument("--state-file", default=".hyp_push_state.json", help="Checkpoint file for resume safety")
    ap.add_argument("--save-every", type=int, default=50, help="Save checkpoint every N processed values")
    args = ap.parse_args()

    if not args.token.strip():
        raise SystemExit("ERROR: provide --token or set HYPOTHESIS_API_TOKEN")

    session = make_session()

    # -----------------------------
    # Determine run_id (stable)
    # -----------------------------
    run_id = args.run_id.strip()
    if not run_id:
        md = find_run_metadata(args.predictions)
        if md and md.get("run_id"):
            run_id = md["run_id"]
    if not run_id:
        for p in iter_predictions(args.predictions):
            if p.get("run_id"):
                run_id = p["run_id"]
                break
    if not run_id:
        raise SystemExit("ERROR: Could not determine run_id. Pass --run-id explicitly.")

    # -----------------------------
    # Load normalised doc index
    # -----------------------------
    norm_idx = load_normalised_index(args.normalised)

    # -----------------------------
    # Work estimate + progress
    # -----------------------------
    total_items = count_work(args.predictions)
    done_items = 0
    print(f"run_id={run_id}")
    print(f"Planned items (non-empty predictions): {total_items}")
    if args.plan:
        print("Mode: PLAN (no uploads, no cleanup)")
    elif args.dry_run:
        print("Mode: DRY-RUN (prints payloads, no uploads, no cleanup)")
    else:
        print("Mode: LIVE upload")

    # -----------------------------
    # Resume/checkpoint state
    # -----------------------------
    state = load_state(args.state_file)
    # We checkpoint by pred_id tag; safe even if ordering changes.
    already_done_pids: Set[str] = set(state.get("done_pred_id_tags", []) or [])

    # -----------------------------
    # Identify bot userid (only for cleanup)
    # -----------------------------
    bot_userid = None
    if (not args.dry_run) and (not args.plan) and (not args.no_cleanup):
        profile = hyp_get_profile(session, args.token)
        bot_userid = profile.get("userid") or profile.get("user") or profile.get("username")

        if not bot_userid:
            print("DEBUG /profile keys:", sorted(profile.keys()))
            print("DEBUG /profile:", json.dumps(profile, indent=2)[:2000])
            raise SystemExit("ERROR: could not determine bot userid from /profile")

        # normalize to acct: form if needed
        if isinstance(bot_userid, str) and "@hypothes.is" in bot_userid and not bot_userid.startswith("acct:"):
            bot_userid = f"acct:{bot_userid}"

    created = 0
    skipped = 0
    deduped = 0
    anchored = 0
    unanchored = 0
    cleaned_total = 0
    processed_values = 0

    # Local dedupe within this script run
    global_seen_pid: Set[str] = set()

    # Helpful periodic status
    last_status_t = time.time()

    for pred_obj in iter_predictions(args.predictions):
        doc_id = pred_obj.get("document_id")
        if not doc_id:
            skipped += 1
            continue

        nrow = norm_idx.get(doc_id) or {}
        uri = nrow.get("canonical_url")
        content_text = nrow.get("content_text") or ""

        if not uri:
            skipped += 1
            continue

        # Prefetch existing pred_id tags ONCE per doc (live/plan). Skipped in dry-run printing mode.
        existing_pid_tags: Set[str] = set()
        if not args.dry_run:
            # In PLAN mode we still prefetch so dedupe counts are accurate.
            existing_pid_tags = collect_existing_pred_ids_for_doc(
                session, args.token, group=args.group, doc_id=doc_id, run_id=run_id
            )

        pred_fields = pred_obj.get("prediction_output_1") or {}
        per_doc_count = 0

        for field, values in pred_fields.items():
            if not isinstance(values, list):
                values = [values]

            for value in values:
                value = (value or "").strip()
                if not value or value == "data not available":
                    continue

                processed_values += 1
                done_items += 1
                per_doc_count += 1

                # progress update (every 10 items)
                if done_items % 10 == 0 or done_items == total_items:
                    progress(done_items, total_items, prefix="Progress: ")

                if per_doc_count > args.max_per_doc:
                    break

                pid = stable_pred_id(run_id, doc_id, field, value)
                pid_tag = f"pred_id:{pid}"

                # Checkpoint/resume dedupe
                if pid_tag in already_done_pids:
                    deduped += 1
                    continue

                # Local dedupe in same run
                if pid_tag in global_seen_pid:
                    deduped += 1
                    continue
                global_seen_pid.add(pid_tag)

                # Dedupe against Hypothesis existing annotations (doc-scoped, across runs)
                if (not args.dry_run) and pid_tag in existing_pid_tags:
                    deduped += 1
                    already_done_pids.add(pid_tag)  # safe to remember
                    continue

                span = find_unique_span(content_text, value)
                is_anchored = span is not None

                tags = [
                    "source:model",
                    "bot:hitl",
                    tag_run(run_id),      # keep run tag for auditability
                    tag_doc(doc_id),
                    tag_field(field),
                    pid_tag,
                    "kind:prediction",
                    "anchored:quote" if is_anchored else "anchored:none",
                ]

                payload = {
                    "group": args.group,
                    "uri": uri,
                    "text": model_text(field, value, anchored=is_anchored),
                    "tags": tags,
                    "permissions": {"read": [f"group:{args.group}"]},
                }

                if is_anchored:
                    exact, prefix, suffix = span  # type: ignore
                    payload["target"] = [
                        {
                            "source": uri,
                            "selector": [
                                {
                                    "type": "TextQuoteSelector",
                                    "exact": exact,
                                    "prefix": prefix,
                                    "suffix": suffix,
                                }
                            ],
                        }
                    ]

                # --- actions ---
                if args.plan:
                    created += 1
                    anchored += int(is_anchored)
                    unanchored += int(not is_anchored)
                elif args.dry_run:
                    print(json.dumps(payload, indent=2))
                    created += 1
                    anchored += int(is_anchored)
                    unanchored += int(not is_anchored)
                else:
                    hyp_create_annotation(session, args.token, payload)
                    created += 1
                    anchored += int(is_anchored)
                    unanchored += int(not is_anchored)

                    # Keep prefetch set up to date so we don't re-post same pid in same doc
                    existing_pid_tags.add(pid_tag)

                # Mark done for resume safety
                already_done_pids.add(pid_tag)

                # Save checkpoint every N processed values (or on first create)
                if args.save_every and (processed_values % args.save_every == 0):
                    state_out = {
                        "run_id": run_id,
                        "done_pred_id_tags": sorted(already_done_pids),
                        "updated_at": time.time(),
                        "stats": {
                            "created": created,
                            "deduped": deduped,
                            "skipped": skipped,
                            "anchored": anchored,
                            "unanchored": unanchored,
                        },
                    }
                    save_state(args.state_file, state_out)

                # periodic status line (every ~15s)
                now = time.time()
                if now - last_status_t > 15:
                    last_status_t = now
                    print(f"\nStatus: created={created} deduped={deduped} skipped={skipped} anchored={anchored} unanchored={unanchored}")

                if args.sleep and (not args.plan) and (not args.dry_run):
                    time.sleep(args.sleep)

        # Cleanup old runs for this doc (LIVE only)
        if (not args.dry_run) and (not args.plan) and (not args.no_cleanup) and bot_userid:
            cleaned = cleanup_old_bot_annotations_for_doc(
                session,
                args.token,
                group=args.group,
                doc_id=doc_id,
                latest_run_id=run_id,
                bot_userid=bot_userid,
                sleep_s=args.sleep,
                dry_run=args.dry_run,
            )
            cleaned_total += cleaned

    # final checkpoint save
    state_out = {
        "run_id": run_id,
        "done_pred_id_tags": sorted(already_done_pids),
        "updated_at": time.time(),
        "stats": {
            "created": created,
            "deduped": deduped,
            "skipped": skipped,
            "anchored": anchored,
            "unanchored": unanchored,
            "cleaned_old_bot_annotations": cleaned_total,
        },
    }
    save_state(args.state_file, state_out)

    print("\n✅ DONE")
    print(f"run_id={run_id}")
    print(f"created={created} deduped={deduped} skipped={skipped}")
    print(f"anchored={anchored} unanchored={unanchored}")
    print(f"cleaned_old_bot_annotations={cleaned_total}")
    print(f"state_file={args.state_file}")
    if bot_userid:
        print(f"bot_userid={bot_userid}")


if __name__ == "__main__":
    main()
