#!/usr/bin/env python3
"""
Delete app-copied HITL annotations from a Hypothesis review group.

Default mode is a dry run. Use --execute to delete from Hypothesis.
Use --clear-db-cache with --execute so the app does not skip recreated items.
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


HYPOTHESIS_API_BASE = "https://api.hypothes.is/api"


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


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=8,
        connect=8,
        read=8,
        status=8,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "DELETE"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def hyp_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.hypothesis.v1+json",
        "Content-Type": "application/json;charset=utf-8",
    }


def get_profile(session: requests.Session, token: str) -> dict:
    response = session.get(f"{HYPOTHESIS_API_BASE}/profile", headers=hyp_headers(token), timeout=60)
    response.raise_for_status()
    return response.json()


def iter_group_annotations(
    session: requests.Session,
    token: str,
    *,
    group_id: str,
    limit: int = 200,
    tag: str = "bot:hitl",
) -> Iterable[dict]:
    params: Dict[str, object] = {
        "group": group_id,
        "tag": tag,
        "sort": "updated",
        "order": "asc",
        "limit": int(limit),
    }
    while True:
        response = session.get(
            f"{HYPOTHESIS_API_BASE}/search",
            params=params,
            headers=hyp_headers(token),
            timeout=60,
        )
        response.raise_for_status()
        rows = response.json().get("rows") or []
        if not rows:
            break
        for row in rows:
            yield row
        cursor = rows[-1].get("updated")
        if not cursor:
            break
        params["search_after"] = cursor


def delete_annotation(session: requests.Session, token: str, annotation_id: str) -> bool:
    response = session.delete(
        f"{HYPOTHESIS_API_BASE}/annotations/{annotation_id}",
        headers=hyp_headers(token),
        timeout=60,
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


def candidate_rows(
    rows: Iterable[dict],
    *,
    bot_userid: str,
    project_id: Optional[str],
    source_tags: set[str],
    any_user: bool,
) -> List[dict]:
    project_tag = f"project_id:{project_id}" if project_id else None
    out: List[dict] = []
    for row in rows:
        tags = {str(t).strip() for t in (row.get("tags") or []) if str(t).strip()}
        if "bot:hitl" not in tags:
            continue
        if tags.isdisjoint(source_tags):
            continue
        if project_tag and project_tag not in tags:
            continue
        if not any_user and row.get("user") != bot_userid:
            continue
        out.append(row)
    return out


def write_snapshot(rows: List[dict], group_id: str, outdir: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(outdir) / f"hypothesis_reset_{group_id}_{stamp}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def clear_db_cache(database_url: str, *, group_id: str, project_id: Optional[str], reset_cursor: bool) -> dict:
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url, pool_pre_ping=True)
    stats = {"project_review_items": 0, "hypothesis_annotations": 0, "project_cursor": 0}
    project_clause = "AND CAST(project_id AS TEXT) = :project_id" if project_id else ""
    tag_clause = "AND tags::text LIKE :project_tag" if project_id else ""
    params = {
        "group_id": group_id,
        "project_id": project_id,
        "project_tag": f"%project_id:{project_id}%" if project_id else None,
    }
    with engine.begin() as conn:
        res = conn.execute(
            text(f"""
                DELETE FROM project_review_items
                WHERE group_id = :group_id
                {project_clause}
            """),
            params,
        )
        stats["project_review_items"] = int(res.rowcount or 0)

        res = conn.execute(
            text(f"""
                DELETE FROM hypothesis_annotations
                WHERE group_id = :group_id
                  AND source_type IN ('model_suggestion', 'gold_reference')
                  {tag_clause}
            """),
            params,
        )
        stats["hypothesis_annotations"] = int(res.rowcount or 0)

        if reset_cursor and project_id:
            res = conn.execute(
                text("""
                    UPDATE project_hypothesis_review_groups
                    SET last_synced_updated = NULL,
                        last_synced_at = NULL,
                        updated_at = NOW()
                    WHERE group_id = :group_id
                      AND CAST(project_id AS TEXT) = :project_id
                """),
                params,
            )
            stats["project_cursor"] = int(res.rowcount or 0)
    return stats


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", required=True, help="Hypothesis group id to reset")
    parser.add_argument("--project-id", default="", help="Optional app project UUID; limits deletion to that project tag")
    parser.add_argument("--token", default=os.getenv("HYPOTHESIS_API_TOKEN", ""), help="Hypothesis API token")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""), help="App database URL")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--outdir", default="data/hypothesis_reset")
    parser.add_argument("--execute", action="store_true", help="Actually delete matching Hypothesis annotations")
    parser.add_argument("--clear-db-cache", action="store_true", help="Also clear local app cache/import rows")
    parser.add_argument("--reset-project-cursor", action="store_true", help="Clear project sync cursor after deletion")
    parser.add_argument("--any-user", action="store_true", help="Delete matching bot-tagged rows even if not owned by this token user")
    parser.add_argument(
        "--source-tags",
        default="source:model_suggestion,source:gold_standard,source:gold_reference",
        help="Comma-separated source tags to delete",
    )
    args = parser.parse_args()

    token = args.token.strip()
    if not token:
        raise SystemExit("ERROR: provide --token or set HYPOTHESIS_API_TOKEN")

    project_id = args.project_id.strip() or None
    source_tags = {t.strip() for t in args.source_tags.split(",") if t.strip()}
    if not source_tags:
        raise SystemExit("ERROR: --source-tags cannot be empty")

    session = make_session()
    profile = get_profile(session, token)
    bot_userid = str(profile.get("userid") or profile.get("user") or profile.get("username") or "")
    if not bot_userid:
        raise SystemExit("ERROR: could not determine the token user's Hypothesis userid")

    rows = list(iter_group_annotations(session, token, group_id=args.group, limit=args.limit))
    candidates = candidate_rows(
        rows,
        bot_userid=bot_userid,
        project_id=project_id,
        source_tags=source_tags,
        any_user=bool(args.any_user),
    )
    snapshot_path = write_snapshot(candidates, args.group, args.outdir)

    print(f"group={args.group}")
    print(f"project_id={project_id or 'ALL'}")
    print(f"matched_copied_annotations={len(candidates)}")
    print(f"snapshot={snapshot_path}")

    for row in candidates[:20]:
        tags = ",".join(row.get("tags") or [])
        print(f"candidate id={row.get('id')} updated={row.get('updated')} tags={tags}")
    if len(candidates) > 20:
        print(f"... {len(candidates) - 20} more candidates")

    if not args.execute:
        print("DRY RUN: no Hypothesis annotations were deleted. Re-run with --execute to delete.")
        return

    deleted = missing = failed = 0
    for row in candidates:
        annotation_id = row.get("id")
        if not annotation_id:
            failed += 1
            continue
        try:
            if delete_annotation(session, token, annotation_id):
                deleted += 1
            else:
                missing += 1
        except Exception as exc:
            failed += 1
            print(f"delete_failed id={annotation_id} error={exc}")
        if args.sleep > 0:
            time.sleep(args.sleep)
    print(f"hypothesis_deleted={deleted} already_missing={missing} failed={failed}")

    if args.clear_db_cache:
        database_url = args.database_url.strip()
        if not database_url:
            raise SystemExit("ERROR: --clear-db-cache requires --database-url or DATABASE_URL")
        stats = clear_db_cache(
            database_url,
            group_id=args.group,
            project_id=project_id,
            reset_cursor=bool(args.reset_project_cursor),
        )
        print(f"db_cleared={json.dumps(stats, sort_keys=True)}")
    else:
        print("DB cache not cleared. Recreate may skip existing item ids unless you clear project_review_items.")


if __name__ == "__main__":
    main()
