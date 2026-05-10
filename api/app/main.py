# api/app/main.py
from __future__ import annotations

import os
import re
import io
import csv
import json
import uuid
import queue
import random
import logging
import hashlib
import threading
import urllib.parse

from datetime import datetime, date, timezone, timedelta
from typing import Any, Dict, List, Optional, Iterable, Tuple
import httpx

from fastapi.responses import HTMLResponse
from fastapi import Request
import requests
from requests.exceptions import RequestException
from requests.adapters import HTTPAdapter

from urllib3.util.retry import Retry

from fastapi import Request, Depends

from uuid import UUID, uuid4
from sqlalchemy import text
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from pydantic import BaseModel, Field, HttpUrl

from sqlalchemy import select, func


import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry



from typing import Any, List, Optional, Union
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import SessionLocal
from .init_db import init
from .models import (
    Team, Project, Document, ProjectDocument,
    HypothesisGroup, HypothesisAnnotation, UserHypothesisWorkspace, ProjectHypothesisReviewGroup,
    Code, CodeAlias, ProjectDocumentReview, TopicRun, DocumentTopic, DocEmbedding,
)

from app.topics.service import get_or_create_active_global_run



logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# App instance MUST be created before mount/include/middleware
# ------------------------------------------------------------------------------
app = FastAPI()

# ------------------------------------------------------------------------------
# Session middleware (configure via env)
# ------------------------------------------------------------------------------
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
_SOLR_SESSION: requests.Session | None = None
# ------------------------------------------------------------------------------
# Static + Templates (mount ONCE)
# ------------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# If your folders are api/app/static and api/app/templates
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.state.templates = Jinja2Templates(directory=TEMPLATES_DIR)

# ------------------------------------------------------------------------------
# Routers (include ONCE)
# ------------------------------------------------------------------------------
from app.auth.router import router as auth_router
from app.ui.routes import router as ui_router

app.include_router(auth_router)
app.include_router(ui_router)


from app.auth.router import router as auth_router

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "CHANGE_ME_TO_LONG_RANDOM_SECRET"),
    same_site="lax",
    https_only=False,  # set True in production behind HTTPS
)

# ------------------------------------------------------------------------------
# Env / constants
# ------------------------------------------------------------------------------
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

SOLR_BASE_URL = os.getenv("SOLR_BASE_URL", "").strip()
HYPOTHESIS_API_TOKEN = os.getenv("HYPOTHESIS_API_TOKEN", "").strip()

DATA_DIR = os.getenv("DATA_DIR", "").strip() or os.path.join(os.getcwd(), "data")
HYPOTHESIS_SNAPSHOT_DIR = os.path.join(DATA_DIR, "hypothesis")
HYPOTHESIS_API_BASE = "https://api.hypothes.is/api"

# Global-core model: projects point to one shared Solr core
SOLR_GLOBAL_CORE = os.getenv("SOLR_GLOBAL_CORE", "hitl_test").strip() or "hitl_test"

# Solr quoting helper
_SOLR_NEEDS_QUOTES = re.compile(r"""[\s:\[\]\(\)\{\}"'\\]""")

# Hypothesis public group safety (NEVER sync __world__ unless explicitly requested)
HYPOTHESIS_PUBLIC_GROUP_ID = "__world__"
HYPOTHESIS_EXCLUDE_PUBLIC = os.getenv("HYPOTHESIS_EXCLUDE_PUBLIC", "true").lower() == "true"
HUMAN_REVIEW_GROUP_ROLES = ("human_workspace", "project_review")


def _env_group_ids(*names: str) -> set[str]:
    ids: set[str] = set()
    for name in names:
        raw = (os.getenv(name) or "").strip()
        if raw:
            ids.add(raw)
    return ids


def infer_hypothesis_group_role(group_id: str | None) -> str:
    gid = (group_id or "").strip()
    if not gid:
        return "unknown"
    if gid == HYPOTHESIS_PUBLIC_GROUP_ID:
        return "public"
    model_ids = _env_group_ids("HYP_GROUP_MODEL", "HYPOTHESIS_MODEL_GROUP_ID")
    model_ids.add("BXp1QL5v")
    gold_ids = _env_group_ids("HYP_GROUP_GOLD", "HYPOTHESIS_GOLD_GROUP_ID")
    gold_ids.add("K48VWwNg")
    if gid in model_ids:
        return "model"
    if gid in gold_ids:
        return "gold"
    if gid in _env_group_ids("HYP_GROUP_SUGGESTIONS", "HYPOTHESIS_SUGGESTION_GROUP_ID"):
        return "model_suggestion"
    return "human_workspace"


def source_type_for_group_role(group_role: str | None) -> str:
    role = (group_role or "unknown").strip()
    if role in {"model", "gold", "model_suggestion"}:
        return role
    if role in HUMAN_REVIEW_GROUP_ROLES:
        return "human"
    return "unknown"


def source_type_for_annotation(fields: dict, group_role: str | None) -> str:
    tags = {str(t).strip().lower() for t in (fields.get("tags") or []) if str(t).strip()}
    if "source:model_suggestion" in tags:
        return "model_suggestion"
    if "source:gold_reference" in tags:
        return "gold_reference"
    if "source:model" in tags:
        return "model"
    if "source:gold" in tags:
        return "gold"
    return source_type_for_group_role(group_role)


def is_human_export_source(source_type: str | None) -> bool:
    return (source_type or "").strip() in {"human", "gold"}


def seed_hypothesis_group_roles(db) -> None:
    role_exportable = {
        "model": False,
        "gold": True,
        "model_suggestion": False,
        "project_review": True,
        "human_workspace": True,
        "unknown": False,
        "public": False,
    }
    known_ids = set()
    known_ids.update(_env_group_ids("HYP_GROUP_MODEL", "HYPOTHESIS_MODEL_GROUP_ID"))
    known_ids.add("BXp1QL5v")
    known_ids.update(_env_group_ids("HYP_GROUP_GOLD", "HYPOTHESIS_GOLD_GROUP_ID"))
    known_ids.add("K48VWwNg")
    known_ids.update(_env_group_ids("HYP_GROUP_SUGGESTIONS", "HYPOTHESIS_SUGGESTION_GROUP_ID"))
    known_ids.add(HYPOTHESIS_PUBLIC_GROUP_ID)

    for gid in known_ids:
        if not gid:
            continue
        row = db.get(HypothesisGroup, gid)
        if not row:
            continue
        role = infer_hypothesis_group_role(gid)
        row.group_role = role
        row.is_exportable = role_exportable.get(role, True)
        if role == "public":
            row.is_enabled = False

    for user_id, group_id in db.execute(select(UserHypothesisWorkspace.user_id, UserHypothesisWorkspace.group_id)).all():
        row = db.get(HypothesisGroup, group_id)
        if not row:
            continue
        if row.group_role in {None, "", "unknown", "human_workspace"}:
            row.group_role = "human_workspace"
            row.owner_user_id = row.owner_user_id or str(user_id)
            row.is_exportable = True

    db.commit()


# import httpx


@app.on_event("startup")
async def app_startup():
    """
    Unified startup:
    - DB init + seeding
    - Snapshot directory
    - External pooled HTTP client (Solr, Hypothesis)
    - Internal ASGI pooled client (UI -> API)
    """

    # --- DB init / seed ---
    init()
    os.makedirs(HYPOTHESIS_SNAPSHOT_DIR, exist_ok=True)

    db = SessionLocal()
    try:
        seed_v1_codes(db)
        seed_code_aliases(db)
        seed_hypothesis_group_roles(db)
    finally:
        db.close()

    # --- External HTTP client (Solr, Hypothesis) ---
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
        ),
        headers={"User-Agent": "hitl-app/1.0"},
    )

    # --- Internal ASGI client (UI -> API calls) ---
    transport = httpx.ASGITransport(app=app)
    app.state.asgi_client = httpx.AsyncClient(
        transport=transport,
        base_url="http://app",
        timeout=httpx.Timeout(60.0),
        limits=httpx.Limits(
            max_connections=200,
            max_keepalive_connections=50,
        ),
    )


@app.on_event("shutdown")
async def app_shutdown():
    """
    Clean shutdown of pooled clients.
    """

    for attr in ("http", "asgi_client"):
        client = getattr(app.state, attr, None)
        if client:
            await client.aclose()


@app.get("/health")
def health():
    return {
        "ok": True,
        "solr": SOLR_BASE_URL,
        "solr_global_core": SOLR_GLOBAL_CORE,
        "data_dir": DATA_DIR,
        "hypothesis_snapshot_dir": HYPOTHESIS_SNAPSHOT_DIR,
        "has_hypothesis_token": bool(HYPOTHESIS_API_TOKEN),
    }



# -----------------------------
# Topic Assignment API models
# -----------------------------
class TopicRunCreateIn(BaseModel):
    project_id: Optional[UUID] = None
    name: str
    topic_schema_version: str = "topics-v1"
    method: str = "external"
    model: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    created_by: Optional[str] = None


class TopicRunOut(BaseModel):
    run_id: Optional[str] = None#str
    project_id: Optional[str]
    name: str
    topic_schema_version: str
    method: str
    model: Optional[str]
    params: Dict[str, Any]
    is_active: bool
    created_at: Optional[str]


class TopicAssign(BaseModel):
    topic_key: str
    topic_label: str
    score: Optional[float] = None
    source: str = "model"
    evidence: Dict[str, Any] = Field(default_factory=dict)


class TopicIngestItem(BaseModel):
    document_id: str
    topics: List[TopicAssign]


class TopicsIngestIn(BaseModel):
    run_id: UUID
    items: List[TopicIngestItem]
    update_solr: bool = True
    # if true, also add schema_versions_ss += topic schema version on docs
    add_schema_version: bool = True


class TopicActivateIn(BaseModel):
    # If true, immediately recompute/push this run into Solr
    push_to_solr: bool = True
    core: str = "hitl_test"




# ------------------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------------------
def parse_dt_utc(val):
    """
    Normalize Hypothesis timestamps to timezone-aware UTC datetimes.
    Accepts ISO8601 strings (with Z or +00:00) or datetime objects.
    Returns None if val is falsy.
    """
    if not val:
        return None

    if isinstance(val, datetime):
        dt = val
    else:
        s = str(val).strip()
        # Hypothesis sometimes uses Z; fromisoformat wants +00:00
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _require_solr() -> None:
    if not SOLR_BASE_URL:
        raise HTTPException(status_code=500, detail="SOLR_BASE_URL is not set on the API server.")


def normalize_url(u: Optional[str]) -> Optional[str]:
    """
    Normalizes URLs so Hypothesis target URLs match ingested canonical URLs.
    - strip fragment
    - lowercase scheme + host
    - remove trailing slash except root
    """
    if not u:
        return None
    u = str(u).strip()
    if not u:
        return None
    try:
        p = urllib.parse.urlsplit(u)
        scheme = (p.scheme or "http").lower()
        netloc = (p.netloc or "").lower()
        path = p.path or "/"
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        # drop fragments
        return urllib.parse.urlunsplit((scheme, netloc, path, p.query, ""))
    except Exception:
        return u.split("#", 1)[0]


# def solr_core_url(core: str) -> str:
#     _require_solr()
#     return f"{SOLR_BASE_URL.rstrip('/')}/{core}"
def solr_core_url(core: str) -> str:
    _require_solr()
    base = SOLR_BASE_URL.rstrip("/")
    if base.endswith("/solr"):
        return f"{base}/{core}"
    return f"{base}/solr/{core}"


def utc_now_z() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def date_to_solr_dt(d: Optional[date]) -> Optional[str]:
    if not d:
        return None
    return f"{d.isoformat()}T00:00:00Z"


def chunked(xs: List[Any], n: int) -> Iterable[List[Any]]:
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


#####################################
# hypotheis utilities for syncing
###################################

# Reuse connections + add robust retries for Hypothesis
_HYP_SESSION = requests.Session()

_HYP_RETRY = Retry(
    total=8,
    connect=8,
    read=8,
    backoff_factor=0.6,  # exponential backoff: 0.6, 1.2, 2.4, ...
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
    respect_retry_after_header=True,
    raise_on_status=False,
)

_HYP_ADAPTER = HTTPAdapter(
    max_retries=_HYP_RETRY,
    pool_connections=10,
    pool_maxsize=10,
)

_HYP_SESSION.mount("https://", _HYP_ADAPTER)
_HYP_SESSION.mount("http://", _HYP_ADAPTER)


def _hyp_get(url: str, *, params: dict | None = None):
    """
    Hypothesis GET with retry/backoff (via urllib3 Retry).
    Uses a (connect, read) timeout to avoid long hangs.
    """
    try:
        return _HYP_SESSION.get(
            url,
            params=params,
            headers=_hyp_headers(),
            timeout=(10, 60),  # connect timeout, read timeout
        )
    except RequestException as e:
        # This includes ConnectionResetError wrapped as ConnectionError, timeouts, etc.
        raise HTTPException(status_code=502, detail=f"Hypothesis request failed: {type(e).__name__}: {e}")


def _hyp_post(url: str, payload: dict):
    try:
        return _HYP_SESSION.post(
            url,
            json=payload,
            headers=_hyp_headers(),
            timeout=(10, 60),
        )
    except RequestException as e:
        raise HTTPException(status_code=502, detail=f"Hypothesis request failed: {type(e).__name__}: {e}")

# ------------------------------------------------------------------------------
# Annotations helpers - code normalisation
# ------------------------------------------------------------------------------

V1_CODES_PATH = os.getenv("V1_CODES_PATH", "/app/data/schema_v1_codes.json")


def load_v1_codes() -> set[str]:
    """
    Loads the canonical v1 list (original 43) from JSON.
    Expected format: {"v1_codes": ["CodeA", "CodeB", ...]}
    """
    try:
        with open(V1_CODES_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        codes = obj.get("v1_codes") or []
        return {str(c).strip() for c in codes if str(c).strip()}
    except Exception:
        return set()


V1_CODES: set[str] = load_v1_codes()



def seed_v1_codes(db):
    """
    Ensures all v1 codes exist in the DB and are locked.
    Safe to run on every startup.
    """
    if not V1_CODES:
        return

    existing = set(
        db.execute(select(Code.code).where(Code.version == "v1")).scalars().all()
    )
    missing = [c for c in V1_CODES if c not in existing]

    for c in missing:
        db.add(
            Code(
                code=c,
                version="v1",
                is_active=True,
                is_locked=True,
            )
        )

    if missing:
        db.commit()




def normalize_tag(t: str) -> str:
    """Light trim only (we keep raw tag for audit; normalization happens in key funcs)."""
    return (t or "").strip()


def canon_key(s: str) -> str:
    """
    A stable comparison key:
    - lowercased
    - remove all non-alphanumerics
    This makes tags like "Confess/Plead" and "confess plead" comparable.
    """
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def levenshtein(a: str, b: str, max_dist: int = 2) -> int:
    """
    Bounded Levenshtein distance.
    Returns > max_dist if distance exceeds threshold (fast exit).
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > max_dist:
        return max_dist + 1
    if not a:
        return len(b)
    if not b:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        row_min = cur[0]
        for j, cb in enumerate(b, start=1):
            ins = cur[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            v = min(ins, dele, sub)
            cur.append(v)
            if v < row_min:
                row_min = v
        prev = cur
        if row_min > max_dist:
            return max_dist + 1
    return prev[-1]


def load_code_maps(db) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """
    Returns:
      - canon_version: canonical_code -> "v1"|"ext" (only active codes)
      - alias_to_canon: alias -> canonical_code
      - key_to_canon: canon_key(canonical_code) -> canonical_code (active only)
    """
    code_rows = db.execute(select(Code.code, Code.version, Code.is_active)).all()
    canon_version: dict[str, str] = {}
    key_to_canon: dict[str, str] = {}

    for code, version, active in code_rows:
        if not active:
            continue
        canon_version[code] = version
        key_to_canon[canon_key(code)] = code

    alias_rows = db.execute(select(CodeAlias.alias, CodeAlias.code)).all()
    alias_to_canon = {normalize_tag(a): c for (a, c) in alias_rows}

    return canon_version, alias_to_canon, key_to_canon


def resolve_tag_to_canonical(
    tag: str,
    canon_version: dict[str, str],
    alias_to_canon: dict[str, str],
    key_to_canon: dict[str, str],
    *,
    fuzzy_max_dist: int = 2,
) -> Optional[str]:
    """
    Resolve a raw Hypothesis tag to a canonical registry code.
    Policy: if we can't confidently resolve, return None (treated as unregistered).
    """
    raw = normalize_tag(tag)
    if not raw:
        return None

    # 1) Exact canonical match
    if raw in canon_version:
        return raw

    # 2) Exact alias match
    if raw in alias_to_canon:
        return alias_to_canon[raw]

    # 3) Normalized key match (punctuation/case differences)
    k = canon_key(raw)
    if k in key_to_canon:
        return key_to_canon[k]

    # 4) Tiny fuzzy match against canonical keys (typos like Appellent)
    # Only accept if best is unique and within threshold
    best_code = None
    best_dist = fuzzy_max_dist + 1
    second_best = fuzzy_max_dist + 1

    for ck, canonical in key_to_canon.items():
        d = levenshtein(k, ck, max_dist=fuzzy_max_dist)
        if d < best_dist:
            second_best = best_dist
            best_dist = d
            best_code = canonical
        elif d < second_best:
            second_best = d

    if best_code is not None and best_dist <= fuzzy_max_dist and best_dist < second_best:
        return best_code

    return None


def split_codes(db, tags: list[str]) -> tuple[set[str], set[str], set[str]]:
    """
    Registry-backed split:
      - v1: canonical codes registered as version="v1"
      - ext: canonical codes registered as version="ext"
      - allc: union
    Unregistered/unresolvable tags are ignored (governance-first).
    """
    canon_version, alias_to_canon, key_to_canon = load_code_maps(db)

    v1: set[str] = set()
    ext: set[str] = set()
    allc: set[str] = set()

    for t in (tags or []):
        canonical = resolve_tag_to_canonical(
            t, canon_version, alias_to_canon, key_to_canon
        )
        if not canonical:
            continue

        allc.add(canonical)
        if canon_version.get(canonical) == "v1":
            v1.add(canonical)
        else:
            ext.add(canonical)

    return v1, ext, allc



def seed_code_aliases(db):
    """
    One-time / idempotent seeding of known legacy Hypothesis tags -> canonical codes.
    Safe to run multiple times.
    """
    mappings = {
        # safe + clear
        "Appellent": "Appellant",
        "Confess/Plead": "ConfessPleadGuilty",
        "WhatAncilliary": "WhatAncillary",
        "Ancillary": "WhatAncillary",
        "AquitOffence": "AcquitOffence",
        "ReasonSentExcess": "ReasonSentExcessNotLenient",
        "ReasonSentLenient": "ReasonSentLenientNotExcess",

        # OPTIONAL (uncomment if you're sure these are intended)
        # "ConvCourtType": "ConvCourtName",
        # "SentCourtType": "SentCourtName",
        # "RSE_is": "ReasonSentExcessNotLenient",
    }

    # only add aliases if target canonical exists
    existing_codes = set(db.execute(select(Code.code)).scalars().all())

    for alias, canonical in mappings.items():
        if canonical not in existing_codes:
            continue
        alias = normalize_tag(alias)
        if not alias:
            continue
        if db.get(CodeAlias, alias):
            continue
        db.add(CodeAlias(alias=alias, code=canonical))

    db.commit()


#######################################################
# CSV export helpers
#######################################################
def iso_z(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    dtu = parse_dt_utc(dt)
    return dtu.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_codes_for_tags_cached(
    tags: list[str],
    canon_version: dict[str, str],
    alias_to_canon: dict[str, str],
    key_to_canon: dict[str, str],
) -> list[tuple[str, str]]:
    """
    Resolve tags -> list of (canonical_code, code_version).
    Uses your same normalization rules as sync.
    Only returns registered active codes.
    """
    out: list[tuple[str, str]] = []
    for t in (tags or []):
        canonical = resolve_tag_to_canonical(
            t,
            canon_version,
            alias_to_canon,
            key_to_canon,
        )
        if not canonical:
            continue
        ver = canon_version.get(canonical)
        if not ver:
            continue
        out.append((canonical, ver))
    return out


def iter_project_document_ids(
    db,
    project_id: str,
    document_id: Optional[str] = None,
    document_ids: Optional[str] = None,
) -> list[str]:
    """
    Returns the target document_ids (scoped to the project membership).
    project_id is UUID in DB, so we normalize it here.

    - If document_id provided: verifies it belongs to project
    - If document_ids provided: intersects with project docs
    - Else: all project docs
    """
    try:
        pid = UUID(str(project_id))
    except Exception:
        raise HTTPException(400, "project_id must be a valid UUID")

    proj_doc_ids = db.execute(
        select(ProjectDocument.document_id).where(ProjectDocument.project_id == pid)
    ).scalars().all()
    proj_set = set(proj_doc_ids)

    if document_id:
        if document_id not in proj_set:
            return []
        return [document_id]

    if document_ids:
        requested = {d.strip() for d in document_ids.split(",") if d.strip()}
        return sorted(list(proj_set & requested))

    return sorted(list(proj_set))


def csv_safe_col(s: str) -> str:
    """
    Convert a canonical code into a stable CSV column name.
    e.g. "ConfessPleadGuilty" -> "code__ConfessPleadGuilty"
         "Confess/Plead" (shouldn't happen after canonical) -> "code__Confess_Plead"
    """
    base = re.sub(r"[^A-Za-z0-9_]+", "_", s).strip("_")
    return f"code__{base}"


# ------------------------------------------------------------------------------
# Solr helpers
# ------------------------------------------------------------------------------

def solr_add_docs(
    core: str,
    docs: List[dict],
    commit: bool = True,
    commit_within_ms: Optional[int] = None,
) -> None:
    params: Dict[str, str] = {}
    if commit:
        params["commit"] = "true"
    if commit_within_ms is not None:
        params["commitWithin"] = str(int(commit_within_ms))

    r = requests.post(
        f"{solr_core_url(core)}/update",
        params=params,
        json=docs,
        timeout=180,
    )
    if r.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"Solr add failed: {r.status_code} {r.text[:800]}")


def solr_atomic_update(core: str, atomic_docs: List[dict], commit_within_ms: int | None = 30000) -> None:
    """
    Atomic update to Solr (pooled).

    - Uses pooled requests.Session via _get_solr_session() (big speed win)
    - Validates payload shape
    - Optional commitWithin (defaults 30s; set None to omit)
    - Surfaces Solr error bodies cleanly
    """
    if not atomic_docs:
        return

    # Validate payload: every doc must include the unique key
    missing_ids: list[int] = []
    doc_ids: list[str] = []
    for i, d in enumerate(atomic_docs):
        if not isinstance(d, dict):
            missing_ids.append(i)
            continue
        did = d.get("document_id_s")
        if not did or not str(did).strip():
            missing_ids.append(i)
        else:
            doc_ids.append(str(did))

    if missing_ids:
        raise HTTPException(
            status_code=500,
            detail=f"Solr atomic update payload invalid: missing/invalid document_id_s at indexes {missing_ids[:50]}",
        )

    url = f"{solr_core_url(core)}/update"
    params = {}
    if commit_within_ms is not None:
        params["commitWithin"] = str(int(commit_within_ms))

    # Use the same pooled session as solr_select()
    sess = _get_solr_session()

    try:
        # (connect, read) timeouts
        timeout = (3.0, 30.0)

        r = sess.post(
            url,
            params=params,
            json=atomic_docs,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )

        if r.status_code >= 300:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "Solr atomic update failed",
                    "core": core,
                    "status": r.status_code,
                    "body": (r.text or "")[:2000],
                    "count": len(atomic_docs),
                    "first_doc_id": doc_ids[0] if doc_ids else None,
                    "last_doc_id": doc_ids[-1] if doc_ids else None,
                },
            )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Solr atomic update error: {type(e).__name__}: {str(e)}",
        ) from e

# def solr_atomic_update(core: str, atomic_docs: List[dict], commit_within_ms: int = 5000) -> None:
#     """
#     Atomic update to Solr.
#
#     Improvements:
#     - Validates payload shape (helps catch silent "nothing updated" bugs).
#     - Adds explicit Content-Type header (some proxies/solr configs are picky).
#     - Logs a small debug summary (counts + first/last doc ids) to diagnose issues.
#     - Surfaces Solr error bodies cleanly.
#
#     IMPORTANT:
#     - Assumes Solr uniqueKey is `document_id_s` (your code uses that everywhere).
#     """
#     if not atomic_docs:
#         return
#
#     # Validate payload: every doc must include the unique key
#     missing_ids: list[int] = []
#     doc_ids: list[str] = []
#     for i, d in enumerate(atomic_docs):
#         if not isinstance(d, dict):
#             missing_ids.append(i)
#             continue
#         did = d.get("document_id_s")
#         if not did or not str(did).strip():
#             missing_ids.append(i)
#         else:
#             doc_ids.append(str(did))
#
#     if missing_ids:
#         raise HTTPException(
#             status_code=500,
#             detail=f"Solr atomic update payload invalid: missing/invalid document_id_s at indexes {missing_ids[:50]}",
#         )
#
#     # Small debug helps massively when updates "succeed" but you see no changes
#     try:
#         logger.debug(
#             "Solr atomic update",
#             extra={
#                 "core": core,
#                 "count": len(atomic_docs),
#                 "commitWithin": int(commit_within_ms),
#                 "first_doc_id": doc_ids[0] if doc_ids else None,
#                 "last_doc_id": doc_ids[-1] if doc_ids else None,
#             },
#         )
#     except Exception:
#         pass
#
#     url = f"{solr_core_url(core)}/update"
#     try:
#         r = requests.post(
#             url,
#             params={"commitWithin": str(int(commit_within_ms))},
#             json=atomic_docs,
#             headers={"Content-Type": "application/json"},
#             timeout=120,
#         )
#         if r.status_code >= 300:
#             raise HTTPException(
#                 status_code=500,
#                 detail=f"Solr atomic update failed: {r.status_code} {r.text[:2000]}",
#             )
#     except HTTPException:
#         raise
#     except Exception as e:
#         raise HTTPException(
#             status_code=500,
#             detail=f"Solr atomic update error: {type(e).__name__}: {str(e)}",
#         ) from e


def solr_update_codes_only(core: str, doc_codes: Dict[str, Dict[str, set[str]]]) -> int:
    """
    Atomic updates for codes_* only.
    Uses 'set' to be deterministic and de-duplicate.
    """
    if not doc_codes:
        return 0

    atomic_docs = []
    for document_id, codes in doc_codes.items():
        atomic_docs.append({
            "document_id_s": document_id,
            "codes_v1_ss": {"set": sorted(codes["v1"])},
            "codes_ext_ss": {"set": sorted(codes["ext"])},
            "codes_all_ss": {"set": sorted(codes["all"])},
        })

    updated = 0
    for batch in chunked(atomic_docs, 500):
        solr_atomic_update(core, batch, commit_within_ms=30000)

        updated += len(batch)

    return updated


def solr_add_project_membership(core: str, project_id: str, document_ids: list[str]) -> int:
    if not document_ids:
        return 0
    atomic_docs = []
    for did in document_ids:
        atomic_docs.append({
            "document_id_s": did,
            "project_ids_ss": {"add": project_id},
        })
    updated = 0
    for batch in chunked(atomic_docs, 500):
        solr_atomic_update(core, batch, commit_within_ms=30000)

        updated += len(batch)
    return updated


def solr_set_project_membership(core: str, doc_to_projects: dict[str, set[str]]) -> int:
    if not doc_to_projects:
        return 0

    atomic_docs = []
    for document_id, pids in doc_to_projects.items():
        atomic_docs.append({
            "document_id_s": document_id,
            "project_ids_ss": {"set": sorted(pids)},
        })

    updated = 0
    for batch in chunked(atomic_docs, 500):
        solr_atomic_update(core, batch, commit_within_ms=30000)

        updated += len(batch)
    return updated


def _get_solr_session() -> requests.Session:
    global _SOLR_SESSION
    if _SOLR_SESSION is not None:
        return _SOLR_SESSION

    s = requests.Session()

    # Keep retries conservative (Solr should be stable)
    retries = Retry(
        total=2,
        backoff_factor=0.1,
        status_forcelist=(502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        pool_connections=20,
        pool_maxsize=50,
        max_retries=retries,
    )
    s.mount("http://", adapter)
    s.mount("https://", adapter)

    _SOLR_SESSION = s
    return s


# def solr_select(core: str, params: dict) -> dict:
#     _require_solr()
#     base = SOLR_BASE_URL.rstrip("/")
#
#     if base.endswith("/solr"):
#         url = f"{base}/{core}/select"
#     else:
#         url = f"{base}/solr/{core}/select"
#
#     def _flatten_params(p: dict) -> list[tuple[str, str]]:
#         out: list[tuple[str, str]] = []
#         for k, v in (p or {}).items():
#             if v is None:
#                 continue
#             if isinstance(v, (list, tuple)):
#                 for item in v:
#                     if item is None:
#                         continue
#                     out.append((str(k), str(item)))
#             else:
#                 out.append((str(k), str(v)))
#         return out
#
#     flat_params = _flatten_params(params)
#
#     # Much tighter timeout: (connect, read)
#     timeout = (3.0, 15.0)
#
#     sess = _get_solr_session()
#     try:
#         r = sess.get(url, params=flat_params, timeout=timeout)
#         r.raise_for_status()
#         return r.json()
#     except requests.HTTPError as e:
#         body = None
#         try:
#             body = r.text
#         except Exception:
#             body = None
#         raise HTTPException(
#             status_code=502,
#             detail={
#                 "error": "Solr request failed",
#                 "core": core,
#                 "url": url,
#                 "status": getattr(r, "status_code", None),
#                 "body": body[:2000] if isinstance(body, str) else body,
#             },
#         ) from e
#     except Exception as e:
#         raise HTTPException(
#             status_code=502,
#             detail={
#                 "error": "Solr request error",
#                 "core": core,
#                 "url": url,
#                 "message": str(e),
#             },
#         ) from e

def solr_select(core: str, params: dict) -> dict:
    _require_solr()
    base = SOLR_BASE_URL.rstrip("/")

    if base.endswith("/solr"):
        url = f"{base}/{core}/select"
    else:
        url = f"{base}/solr/{core}/select"

    def _flatten_params(p: dict) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for k, v in (p or {}).items():
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                for item in v:
                    if item is None:
                        continue
                    out.append((str(k), str(item)))
            else:
                out.append((str(k), str(v)))
        return out

    flat_params = _flatten_params(params)

    # (connect, read)
    timeout = (3.0, 20.0)
    sess = _get_solr_session()

    def _raise_as_http_502(resp: "requests.Response", err: Exception):
        body = None
        try:
            body = resp.text
        except Exception:
            body = None
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Solr request failed",
                "core": core,
                "url": url,
                "status": getattr(resp, "status_code", None),
                "body": body[:2000] if isinstance(body, str) else body,
            },
        ) from err

    try:
        # First try GET (fast path)
        r = sess.get(url, params=flat_params, timeout=timeout)
        if r.status_code == 414:
            # Retry as POST to avoid "URI Too Long"
            r = sess.post(url, data=flat_params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        _raise_as_http_502(r, e)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Solr request error",
                "core": core,
                "url": url,
                "message": str(e),
            },
        ) from e


def solr_escape_term(s: str) -> str:
    # good enough for UUIDs + simple values
    return re.sub(r'([+\-!(){}[\]^"~*?:\\/]|&&|\|\|)', r'\\\1', s)


def normalize_fq_list(fq):
    if fq is None:
        return []
    if isinstance(fq, str):
        return [fq]
    return [x for x in fq if x]


def normalize_fq(fq: str) -> str:
    """
    Normalize fq strings so Solr doesn't 400 when values contain special chars.
    Key fix: if fq is `field:value` and `value` contains ":" (like UUID:done),
    wrap the value in quotes.

    Examples:
      review_status_by_project_ss:UUID:done  -> review_status_by_project_ss:"UUID:done"
      project_ids_ss:UUID                   -> unchanged
      {!tag=x}field:value                   -> unchanged
    """
    if not fq:
        return fq

    fq = fq.strip()

    # leave localparams alone
    if fq.startswith("{!"):
        return fq

    # if already quoted somewhere, don't second-guess
    # (still fine if user passes correct escaping)
    if '"' in fq:
        return fq

    # Only handle simple field:value forms
    if ":" not in fq:
        return fq

    field, value = fq.split(":", 1)
    field = field.strip()
    value = value.strip()

    if not field or not value:
        return fq

    # If the value contains characters that commonly break Solr parsing, quote it.
    # Colon is the big one for your case (UUID:done).
    needs_quotes = (":" in value) or (" " in value) or ("(" in value) or (")" in value)

    if needs_quotes:
        value_escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{field}:"{value_escaped}"'

    return fq



def normalize_fl(fl: Optional[Union[str, List[str]]]) -> Optional[str]:
    """
    Normalize Solr `fl` so callers can pass:
      - fl="a,b,c"
      - fl=["a", "b", "c"]
      - fl=["a,b", "c"]

    Returns:
      - "a,b,c" with:
          * whitespace trimmed
          * empty items removed
          * duplicates removed (preserves first-seen order)
    """
    if fl is None:
        return None

    parts: List[str] = []

    def _add_piece(piece: Any) -> None:
        if piece is None:
            return
        s = str(piece).strip()
        if not s:
            return
        # allow "a,b,c" in one piece
        for p in s.split(","):
            p = p.strip()
            if p:
                parts.append(p)

    if isinstance(fl, list):
        for item in fl:
            _add_piece(item)
    else:
        _add_piece(fl)

    # de-dup while preserving order
    seen = set()
    out: List[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)

    return ",".join(out) if out else None


def solr_update_review_status(core: str, updates: list[dict]) -> int:
    """
    updates: [{"document_id_s": "...", "project_id": "...", "status": "done"}, ...]
    Writes/overwrites one project_id:status entry inside review_status_by_project_ss.

    Implementation approach:
      - query existing review_status_by_project_ss for doc
      - replace matching project_id:* entry
      - atomic set the full list
    """
    if not updates:
        return 0

    # fetch current values for docs in one query
    doc_ids = [u["document_id_s"] for u in updates]
    q = "document_id_s:(" + " ".join(solr_escape_term(x) for x in doc_ids) + ")"

    current = solr_select(core, {
        "q": q,
        "rows": len(doc_ids),
        "fl": "document_id_s,review_status_by_project_ss",
        "wt": "json",
    })
    cur_docs = {d["document_id_s"]: d.get("review_status_by_project_ss", []) for d in current.get("response", {}).get("docs", [])}

    atomic_docs = []
    for u in updates:
        did = u["document_id_s"]
        pid = str(u["project_id"])
        st = u["status"]

        existing = cur_docs.get(did) or []
        if isinstance(existing, str):
            existing = [existing]

        # remove old entry for this project
        new_list = [x for x in existing if not str(x).startswith(pid + ":")]
        new_list.append(f"{pid}:{st}")

        atomic_docs.append({
            "document_id_s": did,
            "review_status_by_project_ss": {"set": sorted(set(new_list))}
        })

    # chunk updates
    updated = 0
    for batch in chunked(atomic_docs, 200):
        solr_atomic_update(core, batch, commit_within_ms=30000)

        updated += len(batch)
    return updated


def solr_update_topics_for_docs(
    core: str,
    doc_to_topics: Dict[str, List[Dict[str, Any]]],
    *,
    run_id: str,
    schema_version: Optional[str] = None,
) -> int:
    """
    doc_to_topics: doc_id -> list of {topic_key, topic_label, score}

    Writes or clears (atomic update):
      - topics_ss         (labels)
      - topic_keys_ss     (keys)
      - topic_kv_ss       (e.g. T01=0.8234)
      - has_topics_b
      - topic_run_id_s
      - schema_versions_ss (optional add)

    IMPORTANT BEHAVIOR FIX:
    - If items is empty for a doc, we clear topics_* AND set topic_run_id_s to run_id,
      so UI and downstream processes can still see the run provenance.
    - We normalize/clean inputs and dedupe while preserving stable sort.
    - We avoid accidental None/"" entries and we enforce string types.
    """
    if not doc_to_topics:
        return 0

    if not run_id or not str(run_id).strip():
        raise ValueError("run_id is required for solr_update_topics_for_docs")

    run_id_s = str(run_id).strip()
    atomic_docs: List[dict] = []

    for doc_id, items in doc_to_topics.items():
        doc_id_s = (doc_id or "").strip()
        if not doc_id_s:
            # skip invalid doc_id keys rather than writing broken docs
            continue

        # ----------------------------------------
        # CASE 1: NO ACTIVE TOPICS → CLEAR IN SOLR
        # ----------------------------------------
        if not items:
            atomic: dict = {
                "document_id_s": doc_id_s,
                "has_topics_b": {"set": False},
                "topic_run_id_s": {"set": run_id_s},
                "topics_ss": {"set": []},
                "topic_keys_ss": {"set": []},
                "topic_kv_ss": {"set": []},
            }
            if schema_version:
                atomic["schema_versions_ss"] = {"add": str(schema_version)}
            atomic_docs.append(atomic)
            continue

        # ----------------------------------------
        # CASE 2: ACTIVE TOPICS → NORMAL SET
        # ----------------------------------------
        labels_set: set[str] = set()
        keys_set: set[str] = set()
        kv_set: set[str] = set()

        for it in items or []:
            if not isinstance(it, dict):
                continue

            k = (it.get("topic_key") or "").strip()
            lab = (it.get("topic_label") or "").strip()
            sc = it.get("score")

            if lab:
                labels_set.add(lab)
            if k:
                keys_set.add(k)

            if k and sc is not None:
                # produce stable token key=value
                try:
                    kv_set.add(f"{k}={float(sc):.4f}")
                except Exception:
                    kv_set.add(f"{k}={str(sc)}")

        labels = sorted(labels_set)
        keys = sorted(keys_set)
        kv = sorted(kv_set)

        atomic2: dict = {
            "document_id_s": doc_id_s,
            "has_topics_b": {"set": True if (labels or keys or kv) else False},
            "topic_run_id_s": {"set": run_id_s},
            "topics_ss": {"set": labels},
            "topic_keys_ss": {"set": keys},
            "topic_kv_ss": {"set": kv},
        }

        if schema_version:
            atomic2["schema_versions_ss"] = {"add": str(schema_version)}

        atomic_docs.append(atomic2)

    updated = 0
    for batch in chunked(atomic_docs, 500):
        solr_atomic_update(core, batch, commit_within_ms=30000)

        updated += len(batch)

    return updated


# ------------------------------------------------------------------------------
# Corpus ingestion Solr doc builder (patched)
# ------------------------------------------------------------------------------

def to_solr_doc(payload: "IngestDocumentIn") -> dict:
    meta = payload.doc_metadata or {}
    if not isinstance(meta, dict):
        meta = {}

    def as_str(v) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        return str(v)

    def as_str_list(v) -> List[str]:
        if v is None:
            return []
        if isinstance(v, list):
            out: List[str] = []
            for x in v:
                s = as_str(x).strip()
                if s:
                    out.append(s)
            return out
        s = as_str(v).strip()
        return [s] if s else []

    published_dt = date_to_solr_dt(payload.published_date)
    canonical = normalize_url(str(payload.canonical_url)) or str(payload.canonical_url)

    solr_doc = {
        "document_id_s": payload.document_id,
        "canonical_url_s": canonical,

        "title_txt": payload.title or "",
        "excerpt_txt": payload.excerpt or "",
        "body_txt": payload.content_text or "",

        "doc_type_s": payload.doc_type or "",
        "source_s": payload.source or "",

        "judges_ss": as_str_list(meta.get("judges")),
        "case_numbers_ss": as_str_list(meta.get("caseNumbers")),
        "citation_references_ss": as_str_list(meta.get("citation_references")),
        "legislation_ss": as_str_list(meta.get("legislation")),

        "citation_s": as_str(meta.get("citation")).strip(),
        "signature_s": as_str(meta.get("signature")).strip(),
        "xml_uri_s": as_str(meta.get("xml_uri")).strip(),
        "file_name_s": as_str(meta.get("file_name")).strip(),
        "appeal_type_s": as_str(meta.get("appeal_type")).strip(),
        "appeal_outcome_s": as_str(meta.get("appeal_outcome")).strip(),

        "schema_versions_ss": [payload.schema_version],

        # Always present so atomic updates + faceting behave consistently
        "project_ids_ss": [],

        "ingested_dt": utc_now_z(),
        "has_human_b": bool(payload.has_human),
        "has_model_b": bool(payload.has_model),
        "has_any_span_b": bool(payload.has_any_span),
        "rand_f": float(payload.rand_f) if payload.rand_f is not None else random.random(),
    }

    keep_empty = {"project_ids_ss", "schema_versions_ss"}
    for k in list(solr_doc.keys()):
        if k in keep_empty:
            continue
        if solr_doc[k] in ("", [], None):
            solr_doc.pop(k)

    if published_dt:
        solr_doc["published_dt"] = published_dt

    return solr_doc


# ------------------------------------------------------------------------------
# Pydantic models
# ------------------------------------------------------------------------------

class IngestDocumentIn(BaseModel):
    document_id: str
    canonical_url: HttpUrl

    published_date: Optional[date] = None
    doc_type: Optional[str] = None
    title: Optional[str] = None
    excerpt: Optional[str] = None
    content_text: str
    source: Optional[str] = None

    doc_metadata: Dict[str, Any] = Field(default_factory=dict, alias="metadata")

    schema_version: str = "hitl-v1"
    has_human: bool = False
    has_model: bool = False
    has_any_span: bool = False
    rand_f: Optional[float] = None

    model_config = {"populate_by_name": True, "extra": "ignore"}


class IngestBatchIn(BaseModel):
    docs: List[Dict[str, Any]]
    commit: bool = False
    commit_within_ms: int = 10_000


class HypothesisSyncRequest(BaseModel):
    core: str = SOLR_GLOBAL_CORE
    group_id: Optional[str] = None
    project_id: Optional[UUID] = None
    all_groups: bool = True
    only_enabled_groups: bool = True
    write_snapshot: bool = True
    limit_per_request: int = 200
    force_full: bool = False

    # NEW: make public syncing opt-in
    include_public: bool = False


class WorkspacePrepareRequest(BaseModel):
    core: str = SOLR_GLOBAL_CORE
    project_id: UUID
    group_id: str
    include_model: bool = True
    include_gold: bool = True
    max_per_doc: int = 80


# Optional: if your frontend expects these (Fix 2)
# class CreateProjectIn(BaseModel):
#     team_id: Optional[str] = None
#     team_name: Optional[str] = None
#     name: str = "Untitled Project"

class CreateProjectIn(BaseModel):
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    name: str = "Untitled Project"
    description: Optional[str] = Field(default=None, max_length=1000)
    creator_user_id: Optional[str] = None  # optional UUID string

class CreateProjectOut(BaseModel):
    team_id: str
    project_id: str
    solr_core: str


class TeamCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)

class ProjectCreateRequest(BaseModel):
    team_id: UUID
    name: str = Field(..., min_length=1)
    description: str | None = Field(default=None, max_length=1000)

class ProjectAddDocsRequest(BaseModel):
    document_ids: list[str] = Field(..., min_length=1)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request):
    # If you want to always show landing page:
    return request.app.state.templates.TemplateResponse(
        "index.html",
        {"request": request, "error": None},
    )

    # OR if you want to redirect to login instead:
    # return RedirectResponse("/auth/login", status_code=303)



# ------------------------------------------------------------------------------
# Ingestion endpoints (Fix 1)
# ------------------------------------------------------------------------------

@app.post("/ingest_batch/{core}")
def ingest_batch(core: str, payload: IngestBatchIn):
    """
    Matches scripts/ingest_jsonl.py which posts to /ingest_batch/{core}.
    Writes docs to Postgres (canonical) and Solr (search).
    """
    db = SessionLocal()
    try:
        to_index: List[dict] = []
        for d in payload.docs:
            doc_in = IngestDocumentIn(**d)

            canon = normalize_url(str(doc_in.canonical_url)) or str(doc_in.canonical_url)

            # --- canonical DB upsert ---
            row = db.get(Document, doc_in.document_id)
            if row:
                row.canonical_url = canon
                row.published_date = doc_in.published_date
                row.doc_type = doc_in.doc_type
                row.title = doc_in.title
                row.excerpt = doc_in.excerpt
                row.content_text = doc_in.content_text
                row.source = doc_in.source
                row.doc_metadata = doc_in.doc_metadata or {}
            else:
                row = Document(
                    document_id=doc_in.document_id,
                    canonical_url=canon,
                    published_date=doc_in.published_date,
                    doc_type=doc_in.doc_type,
                    title=doc_in.title,
                    excerpt=doc_in.excerpt,
                    content_text=doc_in.content_text,
                    source=doc_in.source,
                    doc_metadata=doc_in.doc_metadata or {},
                )
                db.add(row)

            to_index.append(to_solr_doc(doc_in))

        db.commit()

        solr_add_docs(
            core=core,
            docs=to_index,
            commit=bool(payload.commit),
            commit_within_ms=int(payload.commit_within_ms),
        )
        return {"ok": True, "core": core, "indexed": len(to_index), "commit": bool(payload.commit)}
    finally:
        db.close()


@app.post("/solr/{core}/commit")
def solr_commit(core: str):
    """
    Matches scripts calling POST /solr/{core}/commit.
    """
    r = requests.post(
        f"{solr_core_url(core)}/update",
        params={"commit": "true"},
        json=[],
        timeout=60,
    )
    if r.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"Solr commit failed: {r.status_code} {r.text[:800]}")
    return {"ok": True, "core": core}


# ------------------------------------------------------------------------------
# Projects endpoints (Fix 2: global core model)
# ------------------------------------------------------------------------------
#
# @app.post("/projects/bootstrap", response_model=CreateProjectOut)
# def create_project_bootstrap(payload: CreateProjectIn, request: Request):
#     """
#     Global core model: projects do NOT create per-project Solr cores. changed the name
#     create_project_bootstrap from create_project as there is another function
#     """
#     db = SessionLocal()
#     try:
#         # Resolve / create team
#         team = None
#         if payload.team_id:
#             team = db.get(Team, payload.team_id)
#             if not team:
#                 raise HTTPException(status_code=404, detail=f"Team not found: {payload.team_id}")
#
#         if not team:
#             # Try find by name, else create
#             team_name = (payload.team_name or "Default Team").strip()
#             team = db.execute(select(Team).where(Team.name == team_name)).scalars().first()
#             if not team:
#                 team = Team(team_id=str(uuid4()), name=team_name)
#                 db.add(team)
#                 db.commit()
#
#         # proj = Project(project_id=str(uuid4()), team_id=str(team.team_id), name=payload.name)
#         proj = Project(
#             project_id=str(uuid4()),
#             team_id=str(team.team_id),
#             name=(payload.name or "").strip(),
#             description=(payload.description or "").strip() or None,
#         )
#
#         db.add(proj)
#         db.commit()
#
#         # creator_id = None
#
#         user = get_current_user(request)
#         creator_user_id = user.get("user_id")
#         if not creator_user_id:
#             raise HTTPException(500, detail="Session user_id missing")
#
#         if payload.creator_user_id:
#             try:
#                 creator_id = str(UUID(payload.creator_user_id))
#             except Exception:
#                 creator_id = None
#         if payload.creator_user_id:
#             db.execute(
#                 text("""
#                      INSERT INTO project_members (project_id, user_id, role)
#                      VALUES (:pid, :uid, 'owner') ON CONFLICT (project_id, user_id) DO NOTHING
#                      """),
#                 {"pid": str(proj.project_id), "uid": payload.creator_user_id},
#             )
#             db.commit()
#         # Global core: no core creation
#         core_name = SOLR_GLOBAL_CORE
#
#         return CreateProjectOut(
#             team_id=str(team.team_id),
#             project_id=str(proj.project_id),
#             solr_core=core_name,
#         )
#     finally:
#         db.close()

@app.post("/projects/bootstrap", response_model=CreateProjectOut)
def create_project_bootstrap(payload: CreateProjectIn, request: Request):
    """
    Global core model: projects do NOT create per-project Solr cores.
    Also: add the creator as owner in project_members.
    """
    db = SessionLocal()
    try:
        # -----------------------------
        # Team resolve/create
        # -----------------------------
        team = None
        if payload.team_id:
            try:
                team_uuid = UUID(str(payload.team_id))
            except Exception:
                raise HTTPException(status_code=400, detail="team_id must be a valid UUID")
            team = db.get(Team, team_uuid)
            if not team:
                raise HTTPException(status_code=404, detail=f"Team not found: {payload.team_id}")

        if not team:
            team_name = (payload.team_name or "Default Team").strip()
            team = db.execute(select(Team).where(Team.name == team_name)).scalars().first()
            if not team:
                team = Team(team_id=uuid.uuid4(), name=team_name)
                db.add(team)
                db.commit()
                db.refresh(team)

        # -----------------------------
        # Create project
        # -----------------------------
        name_clean = (payload.name or "Untitled Project").strip()
        desc_clean = (payload.description or "").strip() or None

        existing = db.execute(
            select(Project)
            .where(Project.team_id == team.team_id)
            .where(func.lower(Project.name) == name_clean.lower())
        ).scalars().first()

        if existing:
            raise HTTPException(400, "A project with this name already exists for this team")

        proj = Project(
            project_id=uuid.uuid4(),
            team_id=team.team_id,
            name=(payload.name or "Untitled Project").strip(),
            description=(payload.description or "").strip() or None,
        )
        db.add(proj)
        db.commit()
        db.refresh(proj)

        # -----------------------------
        # Membership: add creator as owner
        # -----------------------------
        sess_user = get_current_user(request)
        session_user_id = sess_user.get("id")  # ✅ correct key for your users table

        if not session_user_id:
            raise HTTPException(status_code=500, detail="Session user id missing")

        # Allow override but validate; else use session id
        creator_user_id = session_user_id
        if payload.creator_user_id:
            try:
                creator_user_id = str(UUID(str(payload.creator_user_id)))
            except Exception:
                raise HTTPException(status_code=400, detail="creator_user_id must be a valid UUID")

        # Insert membership row
        db.execute(
            text("""
                INSERT INTO project_members (project_id, user_id, role)
                VALUES (:pid, :uid, 'owner')
                ON CONFLICT (project_id, user_id) DO NOTHING
            """),
            {"pid": str(proj.project_id), "uid": str(creator_user_id)},
        )
        db.commit()

        # Global core: no per-project core
        return CreateProjectOut(
            team_id=str(team.team_id),
            project_id=str(proj.project_id),
            solr_core=SOLR_GLOBAL_CORE,
        )

    finally:
        db.close()


@app.delete("/projects/{project_id}")
def delete_project(project_id: UUID, request: Request, core: str = SOLR_GLOBAL_CORE):
    """
    Delete a project safely.

    This deletes:
    - the project row
    - project document memberships
    - project document review rows
    - project member rows
    - project-scoped topic runs/topics, if any still exist

    It does NOT delete the global Solr core.
    It does NOT delete documents from the global documents table.
    """
    uid = current_user_id(request)

    db = SessionLocal()
    try:
        assert_project_member(db, project_id, uid)

        proj = db.get(Project, project_id)
        if not proj:
            raise HTTPException(404, "Project not found")

        # Capture document IDs before deleting memberships,
        # so we can remove this project_id from Solr project_ids_ss.
        doc_ids = db.execute(
            select(ProjectDocument.document_id)
            .where(ProjectDocument.project_id == project_id)
        ).scalars().all()

        # Delete dependent rows first.
        db.execute(
            text("DELETE FROM project_document_reviews WHERE project_id = :pid"),
            {"pid": str(project_id)},
        )

        db.execute(
            text("DELETE FROM project_documents WHERE project_id = :pid"),
            {"pid": str(project_id)},
        )

        db.execute(
            text("DELETE FROM project_members WHERE project_id = :pid"),
            {"pid": str(project_id)},
        )

        # In the current app topics are mostly user-only, but keep this safe
        # for any older project-scoped topic rows.
        run_ids = db.execute(
            select(TopicRun.run_id).where(TopicRun.project_id == project_id)
        ).scalars().all()

        if run_ids:
            db.execute(
                text("DELETE FROM document_topics WHERE run_id = ANY(:run_ids)"),
                {"run_ids": run_ids},
            )
            db.execute(
                text("DELETE FROM topic_runs WHERE project_id = :pid"),
                {"pid": str(project_id)},
            )

        db.delete(proj)
        db.commit()

        # Best-effort Solr cleanup: remove the deleted project_id from documents.
        solr_docs_updated = 0
        if doc_ids:
            try:
                doc_to_projects = {}
                for did in doc_ids:
                    # This is conservative: set membership to the remaining projects
                    # recorded in Postgres for each document.
                    remaining = db.execute(
                        select(ProjectDocument.project_id)
                        .where(ProjectDocument.document_id == did)
                    ).scalars().all()
                    doc_to_projects[did] = {str(pid) for pid in remaining}

                solr_docs_updated = solr_set_project_membership(core, doc_to_projects)
            except Exception as e:
                logger.exception("Project deleted, but Solr cleanup failed: %s", e)

        return {
            "ok": True,
            "project_id": str(project_id),
            "project_name": proj.name,
            "documents_removed_from_project": len(doc_ids),
            "solr_docs_updated": solr_docs_updated,
            "deleted_solr_core": False,
        }

    finally:
        db.close()
# @app.delete("/projects/{project_id}")
# def delete_project(project_id: UUID, request: Request):
#     uid = current_user_id(request)
#     db = SessionLocal()
#     try:
#         assert_project_member(db, project_id, uid)
#
#         proj = db.get(Project, project_id)
#         if not proj:
#             raise HTTPException(404, "Project not found")
#
#         db.delete(proj)
#         db.commit()
#         return {"ok": True, "project_id": str(project_id), "deleted_solr_core": False}
#     finally:
#         db.close()

# ------------------------------------------------------------------------------
# Hypothesis helpers (patched: URL normalization + bulk resolve)
# ------------------------------------------------------------------------------

def _hyp_headers() -> Dict[str, str]:
    if not HYPOTHESIS_API_TOKEN:
        raise HTTPException(status_code=400, detail="HYPOTHESIS_API_TOKEN is not set on the API server.")
    return {
        "Authorization": f"Bearer {HYPOTHESIS_API_TOKEN}",
        "Accept": "application/vnd.hypothesis.v1+json",
        "Content-Type": "application/json;charset=utf-8",
    }


def hypothesis_get_profile() -> dict:
    r = _hyp_get(f"{HYPOTHESIS_API_BASE}/profile")
    if r.status_code >= 300:
        raise HTTPException(
            status_code=500,
            detail=f"Hypothesis profile failed: {r.status_code} {r.text[:800]}",
        )
    return r.json()


def hypothesis_iter_group_annotations(
    group_id: str,
    limit: int = 200,
    search_after: Optional[str] = None,
    uri: Optional[str] = None,
) -> Iterable[dict]:
    """
    Incremental fetch using search_after on updated.
    Note: search_after should be an ISO8601 string when sorting by updated. :contentReference[oaicite:1]{index=1}
    """
    params = {
        "group": group_id,
        "sort": "updated",
        "order": "asc",
        "limit": int(limit),
    }
    if search_after:
        params["search_after"] = search_after
    if uri:
        params["uri"] = uri

    last_cursor = params.get("search_after")

    while True:
        r = _hyp_get(f"{HYPOTHESIS_API_BASE}/search", params=params)
        if r.status_code >= 300:
            raise HTTPException(
                status_code=500,
                detail=f"Hypothesis search failed: {r.status_code} {r.text[:800]}",
            )

        data = r.json()
        rows = data.get("rows", []) or []
        if not rows:
            break

        for a in rows:
            yield a

        new_cursor = rows[-1].get("updated")
        if not new_cursor:
            break

        # Safety guard: avoid infinite loops if cursor doesn't advance
        if new_cursor == last_cursor:
            break

        params["search_after"] = new_cursor
        last_cursor = new_cursor


def snapshot_path_for_group(group_id: str) -> str:
    day = datetime.utcnow().strftime("%Y-%m-%d")
    day_dir = os.path.join(HYPOTHESIS_SNAPSHOT_DIR, day)
    os.makedirs(day_dir, exist_ok=True)
    return os.path.join(day_dir, f"group_{group_id}.jsonl")


def write_snapshot_jsonl(path: str, annotations: List[dict]) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for a in annotations:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
            n += 1
    return n


def parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def hypothesis_extract(a: dict) -> Tuple[dict, bool, Optional[str]]:
    """
    Returns (fields, has_span, updated_string)
    """
    annotation_id = a.get("id")
    group_id = a.get("group")
    user = a.get("user")
    created_dt = parse_dt(a.get("created"))
    updated_str = a.get("updated")
    updated_dt = parse_dt(updated_str)

    text = a.get("text") or ""
    tags = a.get("tags") or []

    canonical_url = None
    exact = None
    prefix = None
    suffix = None

    targets = a.get("target") or []
    if targets:
        t0 = targets[0]
        canonical_url = normalize_url(t0.get("source"))
        selectors = t0.get("selector") or []
        for sel in selectors:
            if sel.get("type") == "TextQuoteSelector":
                exact = sel.get("exact")
                prefix = sel.get("prefix")
                suffix = sel.get("suffix")
                break

    has_span = bool(exact and str(exact).strip())

    fields = {
        "annotation_id": annotation_id,
        "group_id": group_id,
        "canonical_url": canonical_url,
        "user": user,
        "created": created_dt,
        "updated": updated_dt,
        "text": text,
        "tags": tags,
        "exact": exact,
        "prefix": prefix,
        "suffix": suffix,
        "raw": a,
    }
    return fields, has_span, updated_str


def upsert_group(db, g: dict) -> HypothesisGroup:
    """
    Safeguard: public groups (including __world__) default to disabled.
    Private groups default to enabled.
    Never auto-enable a group that was previously disabled.
    """
    gid = g.get("id")
    name = g.get("name") or gid or ""
    org = g.get("organization")
    scopes = g.get("scopes") or []

    is_public = (gid == HYPOTHESIS_PUBLIC_GROUP_ID) or bool(g.get("public"))
    default_enabled = (not is_public)
    inferred_role = infer_hypothesis_group_role(gid)
    default_exportable = inferred_role in {*HUMAN_REVIEW_GROUP_ROLES, "gold"}

    row = db.get(HypothesisGroup, gid)
    if not row:
        row = HypothesisGroup(
            group_id=gid,
            name=name,
            organization=org,
            scopes=scopes,
            is_enabled=default_enabled,
            group_role=inferred_role,
            is_exportable=default_exportable,
        )
        # Hard-disable __world__ on insert if exclude is on
        if HYPOTHESIS_EXCLUDE_PUBLIC and gid == HYPOTHESIS_PUBLIC_GROUP_ID:
            row.is_enabled = False
        db.add(row)
        return row

    was_personal_placeholder = (
        row.is_enabled is False
        and (row.name or "").startswith("Personal workspace (")
        and not (row.scopes or [])
        and not is_public
    )

    # Update metadata
    row.name = name or row.name
    row.organization = org
    row.scopes = scopes
    if not getattr(row, "group_role", None) or row.group_role == "unknown":
        row.group_role = inferred_role
    if row.group_role in {"model", "model_suggestion", "public"}:
        row.is_exportable = False

    # Don't auto-enable previously disabled groups.
    # If nullable and currently unset, set default.
    # Exception: a user-pasted personal workspace starts as a disabled placeholder.
    # Once the server token can see it in the Hypothesis profile, it is safe to sync.
    if row.is_enabled is None or was_personal_placeholder:
        row.is_enabled = default_enabled

    # Hard-disable __world__ if exclude is on
    if HYPOTHESIS_EXCLUDE_PUBLIC and gid == HYPOTHESIS_PUBLIC_GROUP_ID:
        row.is_enabled = False

    return row


def bulk_resolve_document_ids(db, urls: List[str]) -> Dict[str, str]:
    """
    Resolve canonical_url -> document_id in bulk (with normalization).
    """
    url_to_doc: Dict[str, str] = {}
    if not urls:
        return url_to_doc

    normalized = [normalize_url(u) for u in urls]
    normalized = [u for u in normalized if u]
    if not normalized:
        return url_to_doc

    for batch in chunked(normalized, 1000):
        rows = db.execute(
            select(Document.canonical_url, Document.document_id).where(Document.canonical_url.in_(batch))
        ).all()
        for canon, doc_id in rows:
            url_to_doc[str(canon)] = str(doc_id)

    return url_to_doc


def upsert_annotations_bulk(
    db,
    fields_list: List[dict],
    url_to_doc: Dict[str, str],
    *,
    source_type: str = "human",
    workspace_user_id: Optional[str] = None,
    annotation_status: str = "synced",
    codebook_version: str = "v1",
    model_run_id: Optional[str] = None,
) -> Tuple[int, int, Dict[str, Dict[str, bool]], Dict[str, Dict[str, set[str]]]]:
    """
    Upsert annotations. Returns:
      (annotations_seen, annotations_linked_to_docs, doc_flags_for_solr, doc_codes_for_solr)
    """
    seen = 0
    linked = 0
    doc_flags: Dict[str, Dict[str, bool]] = {}

    # document_id -> {"v1": set(), "ext": set(), "all": set()}
    doc_codes: Dict[str, Dict[str, set[str]]] = {}

    for fields in fields_list:
        seen += 1
        ann_id = fields["annotation_id"]
        effective_source_type = fields.get("source_type") or source_type

        canon = normalize_url(fields.get("canonical_url")) if fields.get("canonical_url") else None
        doc_id = url_to_doc.get(canon) if canon else None

        # Normalize timestamps *before* comparing/storing
        new_updated = parse_dt_utc(fields.get("updated"))
        new_created = parse_dt_utc(fields.get("created"))

        row = db.get(HypothesisAnnotation, ann_id)
        if row:
            row_updated = parse_dt_utc(row.updated)

            # If existing row is newer/equal, skip overwrite
            if row_updated and new_updated and new_updated <= row_updated:
                pass
            else:
                row.group_id = fields["group_id"]
                row.document_id = doc_id
                row.canonical_url = canon
                row.user = fields.get("user")
                row.created = new_created
                row.updated = new_updated
                row.text = fields.get("text")
                row.tags = fields.get("tags") or []
                row.exact = fields.get("exact")
                row.prefix = fields.get("prefix")
                row.suffix = fields.get("suffix")
                row.raw = fields.get("raw") or {}

            row.source_type = effective_source_type
            row.workspace_user_id = workspace_user_id
            row.annotation_status = annotation_status
            row.codebook_version = codebook_version
            row.model_run_id = model_run_id
        else:
            row = HypothesisAnnotation(
                annotation_id=ann_id,
                group_id=fields["group_id"],
                document_id=doc_id,
                canonical_url=canon,
                user=fields.get("user"),
                created=new_created,
                updated=new_updated,
                text=fields.get("text"),
                tags=fields.get("tags") or [],
                exact=fields.get("exact"),
                prefix=fields.get("prefix"),
                suffix=fields.get("suffix"),
                raw=fields.get("raw") or {},
                source_type=effective_source_type,
                workspace_user_id=workspace_user_id,
                annotation_status=annotation_status,
                codebook_version=codebook_version,
                model_run_id=model_run_id,
            )
            db.add(row)

        if doc_id:
            linked += 1

            if not is_human_export_source(effective_source_type):
                continue
            if has_reject_review_marker(fields.get("tags") or []):
                continue

            # Flags
            if doc_id not in doc_flags:
                doc_flags[doc_id] = {"has_human": True, "has_any_span": False}
            if fields.get("exact") and str(fields.get("exact")).strip():
                doc_flags[doc_id]["has_any_span"] = True

            # Codes (registry-backed)
            tags = fields.get("tags") or []
            v1, ext, allc = split_codes(db, tags)

            bucket = doc_codes.setdefault(doc_id, {"v1": set(), "ext": set(), "all": set()})
            bucket["v1"].update(v1)
            bucket["ext"].update(ext)
            bucket["all"].update(allc)

    return seen, linked, doc_flags, doc_codes


def solr_update_flags_for_docs(
    core: str,
    doc_flags: Dict[str, Dict[str, bool]],
    doc_codes: Dict[str, Dict[str, set[str]]] | None = None,
) -> int:
    """
    Chunked Solr atomic updates.
    Returns number of docs updated.
    """
    doc_codes = doc_codes or {}

    if not doc_flags and not doc_codes:
        return 0

    doc_ids = set(doc_flags.keys()) | set(doc_codes.keys())

    atomic_docs = []
    for document_id in doc_ids:
        atomic = {"document_id_s": document_id}

        flags = doc_flags.get(document_id) or {}
        atomic["has_human_b"] = {"set": True}
        if "has_any_span" in flags:
            atomic["has_any_span_b"] = {"set": bool(flags["has_any_span"])}

        codes = doc_codes.get(document_id)
        if codes:
            atomic["codes_v1_ss"] = {"set": sorted(codes["v1"])}
            atomic["codes_ext_ss"] = {"set": sorted(codes["ext"])}
            atomic["codes_all_ss"] = {"set": sorted(codes["all"])}

        atomic_docs.append(atomic)

    updated = 0
    for batch in chunked(atomic_docs, 500):
        solr_atomic_update(core, batch, commit_within_ms=30000)

        updated += len(batch)

    return updated


def acquire_hypothesis_group_sync_lock(db, group_id: str, owner: str, ttl_minutes: int = 20) -> bool:
    locked_until = datetime.utcnow() + timedelta(minutes=ttl_minutes)
    row = db.execute(
        text(
            """
            UPDATE hypothesis_groups
            SET sync_locked_by = :owner,
                sync_locked_until = :locked_until
            WHERE group_id = :group_id
              AND (sync_locked_until IS NULL OR sync_locked_until < NOW())
            RETURNING group_id
            """
        ),
        {"group_id": group_id, "owner": owner, "locked_until": locked_until},
    ).first()
    db.commit()
    return row is not None


def release_hypothesis_group_sync_lock(db, group_id: str, owner: str) -> None:
    db.execute(
        text(
            """
            UPDATE hypothesis_groups
            SET sync_locked_by = NULL,
                sync_locked_until = NULL
            WHERE group_id = :group_id
              AND sync_locked_by = :owner
            """
        ),
        {"group_id": group_id, "owner": owner},
    )
    db.commit()


def project_sync_urls(db, project_id: UUID) -> list[str]:
    rows = (
        db.execute(
            select(Document.canonical_url)
            .join(ProjectDocument, ProjectDocument.document_id == Document.document_id)
            .where(ProjectDocument.project_id == project_id)
            .where(Document.canonical_url.is_not(None))
            .order_by(Document.document_id.asc())
        )
        .scalars()
        .all()
    )
    seen: set[str] = set()
    out: list[str] = []
    for url in rows:
        norm = normalize_url(url)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def stable_workspace_suggestion_id(kind: str, project_id: UUID, doc_id: str, code: str, value: str) -> str:
    raw = json.dumps(
        {
            "kind": kind,
            "project_id": str(project_id),
            "document_id": doc_id,
            "code": code,
            "value": value,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def hypothesis_search_rows(group_id: str, tags: list[str], limit: int = 200) -> list[dict]:
    params: dict[str, Any] = {
        "group": group_id,
        "limit": int(limit),
        "sort": "updated",
        "order": "desc",
    }
    for i, tag in enumerate(tags):
        params["tag" if i == 0 else f"tag{i}"] = tag
    r = _hyp_get(f"{HYPOTHESIS_API_BASE}/search", params=params)
    if r.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"Hypothesis search failed: {r.status_code} {r.text[:800]}")
    return (r.json().get("rows") or [])


def hypothesis_create_annotation(payload: dict) -> dict:
    r = _hyp_post(f"{HYPOTHESIS_API_BASE}/annotations", payload)
    if r.status_code >= 300:
        body = r.text[:800]
        if r.status_code in {400, 403} and "may not create annotations in the specified group" in body:
            raise HTTPException(
                status_code=403,
                detail=(
                    "The server Hypothesis account is not allowed to create annotations in this project review group. "
                    "Invite the server Hypothesis account to the project review group, then run sync again."
                ),
            )
        raise HTTPException(status_code=500, detail=f"Hypothesis create failed: {r.status_code} {r.text[:800]}")
    return r.json()


def hypothesis_profile_userid(profile: dict) -> str:
    return str(profile.get("userid") or profile.get("username") or "the configured server account")


def hypothesis_profile_has_group(profile: dict, group_id: str) -> bool:
    return any((g or {}).get("id") == group_id for g in (profile.get("groups") or []))


def prepare_model_suggestion_payload(
    *,
    group_id: str,
    project_id: UUID,
    doc_id: str,
    uri: str,
    code: str,
    value: str,
) -> tuple[str, dict]:
    sid = stable_workspace_suggestion_id("model", project_id, doc_id, code, value)
    tags = [
        "source:model_suggestion",
        "bot:hitl",
        "status:suggested",
        "implicit_accept:true",
        f"project_id:{project_id}",
        f"doc_id:{doc_id}",
        f"field:{code}",
        f"suggestion_id:{sid}",
    ]
    text = (
        "[MODEL SUGGESTION]\n"
        f"Code: {code}\n"
        f"Suggested value: {value}\n\n"
        "If this is correct, leave it unchanged. To reject or correct it, add your own review annotation "
        "or reply with review:reject / review:corrected in the project review group."
    )
    return sid, {
        "group": group_id,
        "uri": uri,
        "text": text,
        "tags": tags,
        "permissions": {"read": [f"group:{group_id}"]},
    }


def prepare_model_annotation_payload(
    *,
    group_id: str,
    project_id: UUID,
    doc_id: str,
    uri: str,
    annotation: HypothesisAnnotation,
) -> tuple[str, dict]:
    sid = stable_workspace_suggestion_id(
        "model_annotation",
        project_id,
        doc_id,
        annotation.annotation_id,
        annotation.text or annotation.exact or "",
    )
    tags = [
        "source:model_suggestion",
        "bot:hitl",
        "status:suggested",
        "implicit_accept:true",
        f"project_id:{project_id}",
        f"doc_id:{doc_id}",
        f"suggestion_id:{sid}",
    ]
    for tag in annotation.tags or []:
        s = str(tag).strip()
        if s and not s.lower().startswith("source:"):
            tags.append(s)

    text = (
        "[MODEL SUGGESTION]\n"
        f"{annotation.text or annotation.exact or ''}\n\n"
        "If this is correct, leave it unchanged. To reject or correct it, add your own review annotation "
        "or reply with review:reject / review:corrected in the project review group."
    )
    payload = {
        "group": group_id,
        "uri": uri,
        "text": text,
        "tags": tags,
        "permissions": {"read": [f"group:{group_id}"]},
    }
    if annotation.exact:
        payload["target"] = [
            {
                "source": uri,
                "selector": [
                    {
                        "type": "TextQuoteSelector",
                        "exact": annotation.exact,
                        "prefix": annotation.prefix or "",
                        "suffix": annotation.suffix or "",
                    }
                ],
            }
        ]
    return sid, payload


def prepare_gold_reference_payload(
    *,
    group_id: str,
    project_id: UUID,
    doc_id: str,
    uri: str,
    annotation: HypothesisAnnotation,
) -> tuple[str, dict]:
    rid = stable_workspace_suggestion_id(
        "gold",
        project_id,
        doc_id,
        annotation.annotation_id,
        annotation.text or annotation.exact or "",
    )
    tags = [
        "source:gold_reference",
        "bot:hitl",
        "status:reference",
        f"project_id:{project_id}",
        f"doc_id:{doc_id}",
        f"gold_ref_id:{rid}",
    ]
    for tag in annotation.tags or []:
        s = str(tag).strip()
        if s and not s.lower().startswith("source:"):
            tags.append(s)

    text = (
        "[GOLD REFERENCE]\n"
        f"{annotation.text or annotation.exact or ''}\n\n"
        "This is copied into the project review group as reference material. It is not counted as your human annotation."
    )
    payload = {
        "group": group_id,
        "uri": uri,
        "text": text,
        "tags": tags,
        "permissions": {"read": [f"group:{group_id}"]},
    }
    if annotation.exact:
        payload["target"] = [
            {
                "source": uri,
                "selector": [
                    {
                        "type": "TextQuoteSelector",
                        "exact": annotation.exact,
                        "prefix": annotation.prefix or "",
                        "suffix": annotation.suffix or "",
                    }
                ],
            }
        ]
    return rid, payload



# ------------------------------------------------------------------------------
# Progress streaming (SSE)
# ------------------------------------------------------------------------------

def sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def run_hypothesis_sync(payload: HypothesisSyncRequest, emit=None) -> dict:
    """
    Shared implementation used by both /hypothesis/sync and /hypothesis/sync_stream.
    emit: optional function(event_name, data_dict) to push progress.
    """
    def _emit(ev: str, d: dict):
        if emit:
            emit(ev, d)

    db = SessionLocal()
    try:
        _emit("stage", {"msg": "fetch_profile"})
        profile = hypothesis_get_profile()
        groups = profile.get("groups", []) or []

        # Filter out Public group unless explicitly allowed (opt-in)
        exclude_public = HYPOTHESIS_EXCLUDE_PUBLIC and (not payload.include_public)
        if exclude_public:
            groups = [g for g in groups if g.get("id") != HYPOTHESIS_PUBLIC_GROUP_ID]

        _emit("stage", {"msg": "upsert_groups", "count": len(groups)})
        # for g in groups:
            # upsert_group(db, g)
        # db.commit()
        for g in groups:
            row = upsert_group(db, g)
            if payload.force_full and row:
                row.last_synced_updated = None
                row.last_synced_at = None
        db.commit()

        # Decide which groups to sync
        exclude_public = HYPOTHESIS_EXCLUDE_PUBLIC and (not payload.include_public)

        if payload.group_id and not payload.all_groups:
            if exclude_public and payload.group_id == HYPOTHESIS_PUBLIC_GROUP_ID:
                raise HTTPException(
                    status_code=400,
                    detail="Refusing to sync __world__ unless include_public=true."
                )
            group_ids = [payload.group_id]
        else:
            if payload.only_enabled_groups:
                enabled = (
                    db.execute(select(HypothesisGroup).where(HypothesisGroup.is_enabled == True))
                    .scalars()
                    .all()
                )
                group_ids = [g.group_id for g in enabled]
            else:
                group_ids = [g.get("id") for g in groups if g.get("id")]

        # Final hard filter (belt + suspenders)
        exclude_public = HYPOTHESIS_EXCLUDE_PUBLIC and (not payload.include_public)
        if exclude_public:
            group_ids = [gid for gid in group_ids if gid != HYPOTHESIS_PUBLIC_GROUP_ID]

        groups_total = len(group_ids)
        scoped_urls: list[str] = []
        if payload.project_id:
            scoped_urls = project_sync_urls(db, payload.project_id)
            _emit("scope", {
                "project_id": str(payload.project_id),
                "documents_total": len(scoped_urls),
                "mode": "project",
            })

        totals = {
            "groups_synced": 0,
            "groups_skipped_locked": 0,
            "annotations_seen": 0,
            "annotations_linked_to_docs": 0,
            "docs_flagged_in_solr": 0,
        }
        sync_owner = f"sync-{uuid.uuid4()}"

        # Initial progress event (good for initializing a progress bar)
        _emit("progress", {
            "phase": "start",
            "groups_total": groups_total,
            "groups_done": 0,
            "project_id": str(payload.project_id) if payload.project_id else None,
            "documents_total": len(scoped_urls),
            "sync_scope": "project" if payload.project_id else "group",
            "annotations_seen": 0,
            "annotations_linked_to_docs": 0,
            "docs_flagged_in_solr": 0,
        })

        for gi, gid in enumerate(group_ids, start=1):
            lock_acquired = False
            g_row = db.get(HypothesisGroup, gid)
            if not acquire_hypothesis_group_sync_lock(db, gid, sync_owner):
                totals["groups_skipped_locked"] += 1
                _emit("group_skipped", {"group_id": gid, "reason": "sync_locked"})
                continue
            lock_acquired = True

            try:
                g_row = db.get(HypothesisGroup, gid)
                group_role = getattr(g_row, "group_role", None) or infer_hypothesis_group_role(gid)
                source_type = source_type_for_group_role(group_role)
                workspace_user_id = None
                if group_role == "human_workspace":
                    workspace_user_id = (
                        db.execute(
                            select(UserHypothesisWorkspace.user_id)
                            .where(UserHypothesisWorkspace.group_id == gid)
                            .limit(1)
                        )
                        .scalars()
                        .first()
                    )
                cursor = None
                if g_row and not payload.force_full and not scoped_urls:
                    cursor = g_row.last_synced_updated

                _emit("group_start", {
                    "group_id": gid,
                    "group_role": group_role,
                    "source_type": source_type,
                    "i": gi,
                    "n": groups_total,
                    "cursor": cursor,
                })

                # Progress: group start
                _emit("progress", {
                    "phase": "group_start",
                    "group_id": gid,
                    "group_role": group_role,
                    "source_type": source_type,
                    "group_i": gi,
                    "groups_total": groups_total,
                    "groups_done": totals["groups_synced"],
                    "annotations_seen": totals["annotations_seen"],
                    "annotations_linked_to_docs": totals["annotations_linked_to_docs"],
                    "docs_flagged_in_solr": totals["docs_flagged_in_solr"],
                })

                ann_by_id: Dict[str, dict] = {}
                last_updated_seen: Optional[str] = None

                # Fetch annotations (paginated)
                if scoped_urls:
                    for doc_i, uri in enumerate(scoped_urls, start=1):
                        _emit("progress", {
                            "phase": "fetching_project_document",
                            "group_id": gid,
                            "group_i": gi,
                            "groups_total": groups_total,
                            "groups_done": totals["groups_synced"],
                            "document_i": doc_i,
                            "documents_total": len(scoped_urls),
                            "annotations_seen": totals["annotations_seen"],
                            "annotations_linked_to_docs": totals["annotations_linked_to_docs"],
                            "docs_flagged_in_solr": totals["docs_flagged_in_solr"],
                        })
                        for raw in hypothesis_iter_group_annotations(
                            gid,
                            limit=payload.limit_per_request,
                            uri=uri,
                        ):
                            ann_id = raw.get("id")
                            if ann_id:
                                ann_by_id[ann_id] = raw
                            last_updated_seen = raw.get("updated") or last_updated_seen
                else:
                    for raw in hypothesis_iter_group_annotations(
                        gid,
                        limit=payload.limit_per_request,
                        search_after=cursor,
                    ):
                        ann_id = raw.get("id")
                        if ann_id:
                            ann_by_id[ann_id] = raw
                        last_updated_seen = raw.get("updated") or last_updated_seen

                        # Emit periodic progress so clients can show activity
                        if len(ann_by_id) % 500 == 0:
                            _emit("progress", {
                                "phase": "fetching",
                                "group_id": gid,
                                "group_i": gi,
                                "groups_total": groups_total,
                                "groups_done": totals["groups_synced"],
                                "group_annotations_fetched": len(ann_by_id),
                                "annotations_seen": totals["annotations_seen"],
                                "annotations_linked_to_docs": totals["annotations_linked_to_docs"],
                                "docs_flagged_in_solr": totals["docs_flagged_in_solr"],
                            })

                ann_list: List[dict] = list(ann_by_id.values())
                _emit("progress", {
                    "phase": "group_fetched",
                    "group_id": gid,
                    "group_i": gi,
                    "groups_total": groups_total,
                    "groups_done": totals["groups_synced"],
                    "documents_total": len(scoped_urls),
                    "group_annotations_fetched": len(ann_list),
                    "annotations_seen": totals["annotations_seen"],
                    "annotations_linked_to_docs": totals["annotations_linked_to_docs"],
                    "docs_flagged_in_solr": totals["docs_flagged_in_solr"],
                })

                _emit("group_fetched", {"group_id": gid, "annotations_fetched": len(ann_list)})

                if payload.write_snapshot:
                    path = snapshot_path_for_group(gid)
                    write_snapshot_jsonl(path, ann_list)
                    _emit("snapshot", {"group_id": gid, "path": path, "count": len(ann_list)})

                extracted: List[dict] = []
                urls: List[str] = []
                max_updated_str: Optional[str] = None if scoped_urls else cursor

                for raw in ann_list:
                    fields, _has_span, updated_str = hypothesis_extract(raw)
                    fields["source_type"] = source_type_for_annotation(fields, group_role)
                    extracted.append(fields)
                    if fields.get("canonical_url"):
                        urls.append(fields["canonical_url"])
                    if updated_str:
                        max_updated_str = updated_str  # sorted asc, so last wins

                urls_unique = list({normalize_url(u) for u in urls if u})
                urls_unique = [u for u in urls_unique if u]
                _emit("resolve_urls", {"group_id": gid, "unique_urls": len(urls_unique)})

                url_to_doc = bulk_resolve_document_ids(db, urls_unique)
                _emit("resolved", {"group_id": gid, "matched_docs": len(set(url_to_doc.values()))})

                seen, linked, doc_flags, doc_codes = upsert_annotations_bulk(
                    db,
                    extracted,
                    url_to_doc,
                    source_type=source_type_for_group_role(group_role),
                    workspace_user_id=workspace_user_id,
                )
                db.commit()
                updated_docs = solr_update_flags_for_docs(payload.core, doc_flags, doc_codes=doc_codes)

                _emit("codes_summary", {
                    "group_id": gid,
                    "docs_with_codes": len(doc_codes),
                    "docs_with_flags": len(doc_flags),
                    "sample_doc_id": next(iter(doc_codes.keys()), None),
                    "sample_codes_all_count": (len(next(iter(doc_codes.values()))["all"]) if doc_codes else 0),
                })

                if g_row:
                    if max_updated_str and not scoped_urls:
                        g_row.last_synced_updated = max_updated_str
                    g_row.last_synced_at = datetime.utcnow()
                    db.commit()

                totals["groups_synced"] += 1
                totals["annotations_seen"] += seen
                totals["annotations_linked_to_docs"] += linked
                totals["docs_flagged_in_solr"] += updated_docs

                # Progress: group done (this is what moves the bar)
                _emit("progress", {
                    "phase": "group_done",
                    "group_id": gid,
                    "group_i": gi,
                    "groups_total": groups_total,
                    "groups_done": totals["groups_synced"],
                    "annotations_seen": totals["annotations_seen"],
                    "annotations_linked_to_docs": totals["annotations_linked_to_docs"],
                    "docs_flagged_in_solr": totals["docs_flagged_in_solr"],
                })

                _emit("group_done", {
                    "group_id": gid,
                    "group_role": group_role,
                    "source_type": source_type,
                    "annotations_seen": seen,
                    "linked": linked,
                    "docs_flagged": updated_docs,
                    "new_cursor": (g_row.last_synced_updated if g_row else None),
                })
            finally:
                if lock_acquired:
                    release_hypothesis_group_sync_lock(db, gid, sync_owner)

        _emit("done", totals)
        return {
            "ok": True,
            "core": payload.core,
            **totals,
            "snapshot_dir": HYPOTHESIS_SNAPSHOT_DIR if payload.write_snapshot else None,
        }
    finally:
        db.close()


@app.post("/hypothesis/sync")
def hypothesis_sync(payload: HypothesisSyncRequest):
    return run_hypothesis_sync(payload)


@app.post("/hypothesis/prepare_workspace")
def hypothesis_prepare_workspace(payload: WorkspacePrepareRequest, request: Request):
    uid = current_user_id(request)

    db = SessionLocal()
    try:
        assert_project_member(db, payload.project_id, uid)

        review_group = db.get(ProjectHypothesisReviewGroup, payload.project_id)
        if not review_group or review_group.group_id != payload.group_id:
            raise HTTPException(403, "Can only prepare the configured project review group")

        group = db.get(HypothesisGroup, payload.group_id)
        if not group:
            raise HTTPException(404, "Project review group not found")

        profile = hypothesis_get_profile()
        server_userid = hypothesis_profile_userid(profile)
        if not hypothesis_profile_has_group(profile, payload.group_id):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"The server Hypothesis account ({server_userid}) cannot access project review group "
                    f"{payload.group_id}. Hypothesis only allows the server to copy model/gold review items "
                    "into private groups where that account is a member. Invite that account to the project review group, "
                    "then run sync again."
                ),
            )

        doc_rows = (
            db.execute(
                select(Document.document_id, Document.canonical_url)
                .join(ProjectDocument, ProjectDocument.document_id == Document.document_id)
                .where(ProjectDocument.project_id == payload.project_id)
            )
            .all()
        )
        doc_ids = [str(did) for did, _ in doc_rows if did]
        doc_url = {str(did): normalize_url(url) for did, url in doc_rows if did and url}

        created_model = 0
        skipped_model = 0
        created_gold = 0
        skipped_gold = 0
        model_docs_with_annotations: set[str] = set()

        if payload.include_model and doc_ids:
            model_rows = (
                db.execute(
                    select(HypothesisAnnotation)
                    .join(HypothesisGroup, HypothesisGroup.group_id == HypothesisAnnotation.group_id)
                    .where(HypothesisGroup.group_role == "model")
                    .where(HypothesisAnnotation.document_id.in_(doc_ids))
                    .order_by(HypothesisAnnotation.updated.desc().nullslast())
                )
                .scalars()
                .all()
            )
            for ann in model_rows:
                if not ann.document_id:
                    continue
                uri = doc_url.get(ann.document_id) or ann.canonical_url
                if not uri:
                    continue
                model_docs_with_annotations.add(ann.document_id)
                sid, ann_payload = prepare_model_annotation_payload(
                    group_id=payload.group_id,
                    project_id=payload.project_id,
                    doc_id=ann.document_id,
                    uri=uri,
                    annotation=ann,
                )
                existing = hypothesis_search_rows(payload.group_id, [f"suggestion_id:{sid}"], limit=1)
                if existing:
                    skipped_model += 1
                    continue
                hypothesis_create_annotation(ann_payload)
                created_model += 1

            solr_docs = fetch_model_export_docs(payload.core, doc_ids)
            for d in solr_docs:
                doc_id = d.get("document_id_s")
                uri = doc_url.get(doc_id)
                if not doc_id or not uri:
                    continue
                if doc_id in model_docs_with_annotations:
                    continue
                kv_items = d.get("code_value_model_norm_kv_ss") or d.get("code_value_model_kv_ss") or []
                per_doc = 0
                for code, value in parse_solr_kv(kv_items):
                    if per_doc >= max(1, min(int(payload.max_per_doc), 500)):
                        break
                    sid, ann_payload = prepare_model_suggestion_payload(
                        group_id=payload.group_id,
                        project_id=payload.project_id,
                        doc_id=doc_id,
                        uri=uri,
                        code=code,
                        value=value,
                    )
                    existing = hypothesis_search_rows(payload.group_id, [f"suggestion_id:{sid}"], limit=1)
                    if existing:
                        skipped_model += 1
                        continue
                    hypothesis_create_annotation(ann_payload)
                    created_model += 1
                    per_doc += 1

        if payload.include_gold and doc_ids:
            gold_rows = (
                db.execute(
                    select(HypothesisAnnotation)
                    .join(HypothesisGroup, HypothesisGroup.group_id == HypothesisAnnotation.group_id)
                    .where(HypothesisGroup.group_role == "gold")
                    .where(HypothesisAnnotation.document_id.in_(doc_ids))
                    .order_by(HypothesisAnnotation.updated.desc().nullslast())
                )
                .scalars()
                .all()
            )
            for ann in gold_rows:
                if not ann.document_id:
                    continue
                uri = doc_url.get(ann.document_id) or ann.canonical_url
                if not uri:
                    continue
                rid, ann_payload = prepare_gold_reference_payload(
                    group_id=payload.group_id,
                    project_id=payload.project_id,
                    doc_id=ann.document_id,
                    uri=uri,
                    annotation=ann,
                )
                existing = hypothesis_search_rows(payload.group_id, [f"gold_ref_id:{rid}"], limit=1)
                if existing:
                    skipped_gold += 1
                    continue
                hypothesis_create_annotation(ann_payload)
                created_gold += 1

        return {
            "ok": True,
            "project_id": str(payload.project_id),
            "group_id": payload.group_id,
            "documents": len(doc_ids),
            "created_model_suggestions": created_model,
            "skipped_existing_model_suggestions": skipped_model,
            "created_gold_references": created_gold,
            "skipped_existing_gold_references": skipped_gold,
        }
    finally:
        db.close()


@app.post("/hypothesis/sync_stream")
def hypothesis_sync_stream(payload: HypothesisSyncRequest):
    """
    Streams progress events (SSE) in real-time.
    """
    def gen():
        q: "queue.Queue[str]" = queue.Queue()
        done = threading.Event()

        def emit(ev: str, d: dict):
            q.put(sse_event(ev, d))

        def worker():
            try:
                result = run_hypothesis_sync(payload, emit=emit)
                q.put(sse_event("result", result))
            except Exception as e:
                logger.exception("hypothesis sync failed")
                q.put(sse_event("error", {"error": str(e)}))
            finally:
                done.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        # Stream events as they arrive. Send a heartbeat so proxies don't buffer.
        while not done.is_set() or not q.empty():
            try:
                msg = q.get(timeout=1.0)
                yield msg
            except queue.Empty:
                # heartbeat comment line for SSE
                yield ": ping\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


############## Progress sync without hypothesis download##################
# Recompute codes for everything
# curl -sS -X POST "http://localhost:8000/solr/recompute_codes?core=hitl_test"
#
# Recompute codes only for one group
# curl -sS -X POST "http://localhost:8000/solr/recompute_codes?core=hitl_test&group_id=Qb9zgyQY"

@app.post("/solr/recompute_codes")
def recompute_solr_codes(
    core: str = "hitl_test",
    project_id: Optional[UUID] = None,
    group_id: Optional[str] = None,
    source: str = "human",
):
    """
    Recompute Solr codes_* purely from Postgres hypothesis_annotations.
    No Hypothesis API calls.

    Optional filters:
      - project_id: only docs in that project
      - group_id: only annotations from that Hypothesis group
    """
    if source not in {"human", "gold", "all"}:
        raise HTTPException(400, "source must be human|gold|all")

    db = SessionLocal()
    try:
        # load code maps once
        canon_version, alias_to_canon, key_to_canon = load_code_maps(db)

        # determine doc scope (optional project filter)
        doc_scope: Optional[set[str]] = None
        if project_id:
            doc_scope = set(
                db.execute(select(ProjectDocument.document_id).where(ProjectDocument.project_id == project_id)).scalars().all()
            )

        stmt = (
            select(
                HypothesisAnnotation.document_id,
                HypothesisAnnotation.tags,
            )
            .join(HypothesisGroup, HypothesisGroup.group_id == HypothesisAnnotation.group_id)
            .where(HypothesisAnnotation.document_id.is_not(None))
            .where(HypothesisGroup.is_exportable == True)
        )

        if source == "human":
            stmt = stmt.where(HypothesisGroup.group_role.in_(HUMAN_REVIEW_GROUP_ROLES))
            stmt = stmt.where(HypothesisAnnotation.source_type == "human")
        elif source == "gold":
            stmt = stmt.where(HypothesisGroup.group_role == "gold")
            stmt = stmt.where(HypothesisAnnotation.source_type == "gold")
        else:
            stmt = stmt.where(HypothesisGroup.group_role.in_([*HUMAN_REVIEW_GROUP_ROLES, "gold"]))
            stmt = stmt.where(HypothesisAnnotation.source_type.in_(["human", "gold"]))

        if group_id:
            stmt = stmt.where(HypothesisAnnotation.group_id == group_id)

        doc_codes: dict[str, dict[str, set[str]]] = {}

        scanned = 0
        for doc_id, tags in db.execute(stmt).yield_per(5000):
            scanned += 1
            if not doc_id:
                continue
            if has_reject_review_marker(tags):
                continue
            if doc_scope is not None and doc_id not in doc_scope:
                continue

            v1, ext, allc = set(), set(), set()
            # resolve tags -> canonical codes (registry + aliases + fuzzy)
            for raw in (tags or []):
                canonical = resolve_tag_to_canonical(raw, canon_version, alias_to_canon, key_to_canon)
                if not canonical:
                    continue
                ver = canon_version.get(canonical)
                if not ver:
                    continue
                allc.add(canonical)
                if ver == "v1":
                    v1.add(canonical)
                else:
                    ext.add(canonical)

            if not allc:
                continue

            bucket = doc_codes.setdefault(doc_id, {"v1": set(), "ext": set(), "all": set()})
            bucket["v1"].update(v1)
            bucket["ext"].update(ext)
            bucket["all"].update(allc)

        updated = solr_update_codes_only(core, doc_codes)

        return {
            "ok": True,
            "core": core,
            "project_id": str(project_id) if project_id else None,
            "group_id": group_id,
            "source": source,
            "annotation_rows_scanned": scanned,
            "docs_with_codes": len(doc_codes),
            "docs_updated_in_solr": updated,
        }
    finally:
        db.close()

@app.post("/solr/recompute_projects")
def recompute_solr_projects(core: str = "hitl_test", project_id: Optional[UUID] = None):
    """
    Recompute Solr project_ids_ss purely from Postgres project_documents.
    If project_id is provided: only that project is applied.
    If not provided: rebuild for all project_documents.
    """
    db = SessionLocal()
    try:
        stmt = select(ProjectDocument.project_id, ProjectDocument.document_id)
        if project_id:
            stmt = stmt.where(ProjectDocument.project_id == project_id)

        rows = db.execute(stmt).all()

        doc_to_projects: dict[str, set[str]] = {}
        for pid, did in rows:
            if not did:
                continue
            bucket = doc_to_projects.setdefault(did, set())
            bucket.add(str(pid))

        updated = solr_set_project_membership(core, doc_to_projects)

        return {
            "ok": True,
            "core": core,
            "project_id": str(project_id) if project_id else None,
            "project_document_rows_scanned": len(rows),
            "docs_updated_in_solr": updated,
        }
    finally:
        db.close()


@app.post("/solr/recompute_project_membership")
def solr_recompute_project_membership(
    project_id: UUID,
    core: str = "hitl_test",
):
    """
    Push project_documents membership into Solr.project_ids_ss for all docs in the project.
    Useful after SQL bootstraps or rebuilds.
    """
    db = SessionLocal()
    try:
        doc_ids = db.execute(
            select(ProjectDocument.document_id)
            .where(ProjectDocument.project_id == project_id)
        ).scalars().all()

        if not doc_ids:
            return {"ok": True, "project_id": str(project_id), "docs_in_project": 0, "solr_updated": 0}

        atomic_docs = [{"document_id_s": did, "project_ids_ss": {"add": str(project_id)}} for did in doc_ids]

        updated = 0
        for batch in chunked(atomic_docs, 500):
            solr_atomic_update(core, batch, commit_within_ms=30000)

            updated += len(batch)

        return {"ok": True, "core": core, "project_id": str(project_id), "docs_in_project": len(doc_ids), "solr_updated": updated}
    finally:
        db.close()


##Add the minimal code-registry API (optional but recommended)

# If you want users/admins to actually “Add Codes” without touching DB manually.

##
class CodeCreate(BaseModel):
    code: str
    display_name: Optional[str] = None
    description: Optional[str] = None


class AliasCreate(BaseModel):
    alias: str


@app.get("/codes")
def list_codes(include_inactive: bool = False):
    db = SessionLocal()
    try:
        q = select(Code)
        if not include_inactive:
            q = q.where(Code.is_active == True)

        codes = db.execute(q).scalars().all()
        out = []
        for c in codes:
            aliases = db.execute(select(CodeAlias.alias).where(CodeAlias.code == c.code)).scalars().all()
            out.append({
                "code": c.code,
                "version": c.version,
                "display_name": c.display_name,
                "description": c.description,
                "is_active": c.is_active,
                "is_locked": c.is_locked,
                "aliases": aliases,
            })
        return {"codes": out}
    finally:
        db.close()


@app.post("/codes")
def create_code(payload: CodeCreate):
    db = SessionLocal()
    try:
        code = normalize_tag(payload.code)
        if not code:
            raise HTTPException(400, "code is empty")

        if db.get(Code, code):
            raise HTTPException(409, "code already exists")

        row = Code(
            code=code,
            version="ext",
            display_name=payload.display_name,
            description=payload.description,
            is_active=True,
            is_locked=False,
        )
        db.add(row)
        db.commit()
        return {"ok": True, "code": code, "version": "ext"}
    finally:
        db.close()


@app.post("/codes/{code}/aliases")
def add_alias(code: str, payload: AliasCreate):
    db = SessionLocal()
    try:
        code = normalize_tag(code)
        alias = normalize_tag(payload.alias)

        c = db.get(Code, code)
        if not c or not c.is_active:
            raise HTTPException(404, "unknown code")

        if db.get(CodeAlias, alias):
            raise HTTPException(409, "alias already exists")

        db.add(CodeAlias(alias=alias, code=code))
        db.commit()
        return {"ok": True, "code": code, "alias": alias}
    finally:
        db.close()


@app.patch("/codes/{code}/deactivate")
def deactivate_code(code: str):
    db = SessionLocal()
    try:
        code = normalize_tag(code)
        row = db.get(Code, code)
        if not row:
            raise HTTPException(404, "unknown code")
        if row.is_locked:
            raise HTTPException(400, "cannot deactivate locked v1 code")

        row.is_active = False
        db.commit()
        return {"ok": True, "code": code}
    finally:
        db.close()


################ CSV export end point##############
# 4) How to run it
# Export all docs in a project
# curl -L -o out.csv "http://localhost:8000/export/csv?project_id=YOUR_PROJECT_ID"
#
# Export a single document
# curl -L -o out.csv "http://localhost:8000/export/csv?project_id=YOUR_PROJECT_ID&document_id=DOC_ID"
#
# Export selected docs
# curl -L -o out.csv "http://localhost:8000/export/csv?project_id=YOUR_PROJECT_ID&document_ids=DOC1,DOC2,DOC3"
#
# Export only one code (by canonical name or alias/variant)
# curl -L -o out.csv "http://localhost:8000/export/csv?project_id=YOUR_PROJECT_ID&code=Confess/Plead"
#
# Export only v1 codes
# curl -L -o out.csv ("http://localhost:8000/export/csv?project_id=YOUR_PROJECT_ID&version=v1"

################################################################################################
def _normalize_tags(tags: Any) -> list[str]:
    """
    Make HypothesisAnnotation.tags safe/consistent as list[str].

    Handles:
      - list[str]
      - tuple[str]
      - JSON string: '["A","B"]'
      - None
      - other -> best-effort
    """
    if tags is None:
        return []

    # Already list-like
    if isinstance(tags, (list, tuple)):
        out: list[str] = []
        for t in tags:
            if t is None:
                continue
            s = str(t).strip()
            if s:
                out.append(s)
        return out

    # If psycopg/SQLAlchemy returns JSON as string
    if isinstance(tags, str):
        s = tags.strip()
        if not s:
            return []
        # attempt JSON parse
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
        # fallback: treat as single tag
        return [s]

    # dict / other types (rare)
    try:
        return [str(tags).strip()] if str(tags).strip() else []
    except Exception:
        return []


REVIEW_REJECT_TAGS = {"review:reject", "review:rejected", "status:rejected", "reject"}


def has_reject_review_marker(tags: Any) -> bool:
    tag_set = {str(t).strip().lower() for t in _normalize_tags(tags)}
    return bool(tag_set & REVIEW_REJECT_TAGS)


def codes_from_tags_and_field_markers(
    tags: Any,
    canon_version: dict[str, str],
    alias_to_canon: dict[str, str],
    key_to_canon: dict[str, str],
) -> list[tuple[str, str]]:
    tag_list = _normalize_tags(tags)
    resolved = resolve_codes_for_tags_cached(tag_list, canon_version, alias_to_canon, key_to_canon)
    seen = {(code, ver) for code, ver in resolved}

    for tag in tag_list:
        if not tag.lower().startswith("field:"):
            continue
        field_value = tag.split(":", 1)[1].strip()
        if not field_value:
            continue
        canonical = resolve_tag_to_canonical(field_value, canon_version, alias_to_canon, key_to_canon)
        if not canonical:
            continue
        code_ver = canon_version.get(canonical)
        if code_ver:
            seen.add((canonical, code_ver))

    return sorted(seen)


def collect_rejected_review_codes(
    db,
    doc_ids: list[str],
    user_id: str,
    canon_version: dict[str, str],
    alias_to_canon: dict[str, str],
    key_to_canon: dict[str, str],
    *,
    version: str = "all",
    code_filter: Optional[str] = None,
) -> set[tuple[str, str]]:
    stmt = (
        select(HypothesisAnnotation.document_id, HypothesisAnnotation.tags)
        .join(HypothesisGroup, HypothesisGroup.group_id == HypothesisAnnotation.group_id)
        .where(HypothesisAnnotation.document_id.in_(doc_ids))
        .where(HypothesisGroup.group_role.in_(HUMAN_REVIEW_GROUP_ROLES))
        .where(HypothesisAnnotation.source_type == "human")
    )

    rejected: set[tuple[str, str]] = set()
    for doc_id, tags in db.execute(stmt).yield_per(5000):
        if not doc_id or not has_reject_review_marker(tags):
            continue
        for canonical_code, code_ver in codes_from_tags_and_field_markers(
            tags,
            canon_version,
            alias_to_canon,
            key_to_canon,
        ):
            if code_filter and canonical_code != code_filter:
                continue
            if version != "all" and code_ver != version:
                continue
            rejected.add((doc_id, canonical_code))

    return rejected


def build_wide_aggregates(
    db,
    doc_ids: list[str],
    canon_version: dict[str, str],
    alias_to_canon: dict[str, str],
    key_to_canon: dict[str, str],
    *,
    version: str = "all",
    code_filter: Optional[str] = None,
    source_filter: str = "human",
) -> tuple[dict[str, dict[str, dict]], set[str]]:
    """
    Build per-doc aggregates and the set of codes seen, for /export/csv_wide.

    Returns:
      per_doc: {doc_id: {canonical_code: {latest_value, latest_updated, count}}}
      codes_seen: set of canonical_code
    """
    per_doc: dict[str, dict[str, dict]] = {}
    codes_seen: set[str] = set()

    stmt = (
        select(
            HypothesisAnnotation.document_id,
            HypothesisAnnotation.tags,
            HypothesisAnnotation.text,
            HypothesisAnnotation.updated,
        )
        .join(HypothesisGroup, HypothesisGroup.group_id == HypothesisAnnotation.group_id)
        .where(HypothesisAnnotation.document_id.in_(doc_ids))
        .where(HypothesisGroup.is_exportable == True)
    )

    if source_filter == "human":
        stmt = stmt.where(HypothesisGroup.group_role.in_(HUMAN_REVIEW_GROUP_ROLES))
        stmt = stmt.where(HypothesisAnnotation.source_type == "human")
    elif source_filter == "gold":
        stmt = stmt.where(HypothesisGroup.group_role == "gold")
        stmt = stmt.where(HypothesisAnnotation.source_type == "gold")
    else:
        stmt = stmt.where(HypothesisGroup.group_role.in_([*HUMAN_REVIEW_GROUP_ROLES, "gold"]))
        stmt = stmt.where(HypothesisAnnotation.source_type.in_(["human", "gold"]))

    for doc_id, tags, text, updated in db.execute(stmt).yield_per(5000):
        if not doc_id:
            continue

        tag_list = _normalize_tags(tags)
        if has_reject_review_marker(tag_list):
            continue

        resolved = resolve_codes_for_tags_cached(
            tag_list, canon_version, alias_to_canon, key_to_canon
        )
        if not resolved:
            continue

        val = (text or "").strip()
        upd = parse_dt_utc(updated)

        bucket = per_doc.setdefault(doc_id, {})

        for canonical_code, code_ver in resolved:
            if code_filter and canonical_code != code_filter:
                continue
            if version != "all" and code_ver != version:
                continue

            codes_seen.add(canonical_code)

            rec = bucket.get(canonical_code)
            if not rec:
                rec = {
                    "count": 0,
                    "latest_value": None,
                    "latest_updated": None,
                }
                bucket[canonical_code] = rec

            rec["count"] += 1

            if val:
                if upd:
                    if rec["latest_updated"] is None or upd > rec["latest_updated"]:
                        rec["latest_updated"] = upd
                        rec["latest_value"] = val
                else:
                    if rec["latest_value"] is None:
                        rec["latest_value"] = val

    return per_doc, codes_seen


################ Export CSV ##################

def parse_solr_kv(items: list[str] | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for s in items or []:
        if not s or "=" not in s:
            continue
        code, value = s.split("=", 1)
        code = (code or "").strip()
        value = (value or "").strip()
        if code and value:
            out.append((code, value))
    return out


def build_model_long_aggregates(solr_docs, *, code_filter: Optional[str] = None):
    agg: dict[tuple[str, str], dict] = {}

    for d in solr_docs:
        doc_id = d.get("document_id_s")
        if not doc_id:
            continue

        kv_items = d.get("code_value_model_norm_kv_ss") or d.get("code_value_model_kv_ss") or []
        pairs = parse_solr_kv(kv_items)

        for canonical_code, value in pairs:
            if code_filter and canonical_code != code_filter:
                continue

            key = (doc_id, canonical_code)
            rec = agg.get(key)
            if not rec:
                rec = {
                    "code_version": "unknown",
                    "values_set": set(),
                    "latest_value": None,
                }
                agg[key] = rec

            rec["values_set"].add(value)
            if rec["latest_value"] is None:
                rec["latest_value"] = value

    return agg


def build_model_wide_aggregates(solr_docs, *, code_filter: Optional[str] = None):
    per_doc: dict[str, dict[str, dict]] = {}
    codes_seen: set[str] = set()

    for d in solr_docs:
        doc_id = d.get("document_id_s")
        if not doc_id:
            continue

        kv_items = d.get("code_value_model_norm_kv_ss") or d.get("code_value_model_kv_ss") or []
        pairs = parse_solr_kv(kv_items)

        bucket = per_doc.setdefault(doc_id, {})
        for canonical_code, value in pairs:
            if code_filter and canonical_code != code_filter:
                continue

            codes_seen.add(canonical_code)
            bucket[canonical_code] = {
                "count": 1,
                "latest_value": value,
                "latest_updated": None,
            }

    return per_doc, codes_seen

def fetch_model_export_docs(core: str, doc_ids: list[str]) -> list[dict]:
    if not doc_ids:
        return []

    url = f"{SOLR_BASE_URL}/{core}/select"
    rows: list[dict] = []

    chunk_size = 500
    for i in range(0, len(doc_ids), chunk_size):
        chunk = doc_ids[i:i + chunk_size]
        fq = "(" + " OR ".join([f'document_id_s:"{x}"' for x in chunk]) + ")"

        params = {
            "q": "*:*",
            "fq": fq,
            "rows": len(chunk),
            "fl": "document_id_s,code_value_model_kv_ss,code_value_model_norm_kv_ss,has_model_b",
            "wt": "json",
        }
        r = requests.get(url, params=params, timeout=60)
        r.raise_for_status()
        rows.extend(r.json().get("response", {}).get("docs", []))

    return rows



@app.get("/export/csv")
def export_csv(
    request: Request,
    project_id: UUID,
    core: str = "hitl_test",
    document_id: Optional[str] = None,
    document_ids: Optional[str] = None,
    code: Optional[str] = None,
    version: str = "all",
    source: str = "all",
    include_annotators: bool = False,
):
    if source not in {"reviewed", "human", "gold", "model", "all"}:
        raise HTTPException(400, "source must be reviewed|human|gold|model|all")
    if version not in {"v1", "ext", "all"}:
        raise HTTPException(400, "version must be v1|ext|all")

    uid = current_user_id(request)

    db = SessionLocal()
    try:
        assert_project_member(db, project_id, uid)

        doc_ids = iter_project_document_ids(
            db,
            str(project_id),
            document_id=document_id,
            document_ids=document_ids,
        )
        doc_ids = list(doc_ids) if doc_ids else []
        if not doc_ids:
            raise HTTPException(404, "No documents matched")

        doc_rows = db.execute(
            select(Document.document_id, Document.canonical_url).where(Document.document_id.in_(doc_ids))
        ).all()
        doc_url = {d: u for (d, u) in doc_rows}

        canon_version, alias_to_canon, key_to_canon = load_code_maps(db)

        code_filter: Optional[str] = None
        if code:
            cf = resolve_tag_to_canonical(code, canon_version, alias_to_canon, key_to_canon)
            code_filter = cf or "__NO_MATCH__"

        headers = [
            "project_id", "document_id", "canonical_url", "code", "code_version", "source",
            "value", "value_mode", "values", "n_values", "has_span", "span_examples",
            "n_annotations", "latest_updated",
        ]
        if include_annotators:
            headers.append("annotators")

        def gen():
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=headers)
            w.writeheader()
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

            if code_filter == "__NO_MATCH__":
                return

            # -------------------------
            # Human aggregates
            # -------------------------
            human_agg: dict[tuple[str, str, str], dict] = {}

            if source in {"reviewed", "human", "gold", "all"}:
                stmt = (
                    select(
                        HypothesisAnnotation.document_id,
                        HypothesisAnnotation.tags,
                        HypothesisAnnotation.text,
                        HypothesisAnnotation.exact,
                        HypothesisAnnotation.user,
                        HypothesisAnnotation.updated,
                        HypothesisGroup.group_role,
                    )
                    .join(HypothesisGroup, HypothesisGroup.group_id == HypothesisAnnotation.group_id)
                    .where(HypothesisAnnotation.document_id.in_(doc_ids))
                    .where(HypothesisGroup.is_exportable == True)
                )

                if source == "human":
                    stmt = stmt.where(HypothesisGroup.group_role.in_(HUMAN_REVIEW_GROUP_ROLES))
                    stmt = stmt.where(HypothesisAnnotation.source_type == "human")
                elif source == "gold":
                    stmt = stmt.where(HypothesisGroup.group_role == "gold")
                    stmt = stmt.where(HypothesisAnnotation.source_type == "gold")
                else:
                    stmt = stmt.where(HypothesisGroup.group_role.in_([*HUMAN_REVIEW_GROUP_ROLES, "gold"]))
                    stmt = stmt.where(HypothesisAnnotation.source_type.in_(["human", "gold"]))

                for doc_id, tags, text, exact, user_, updated, group_role in db.execute(stmt).yield_per(5000):
                    if not doc_id:
                        continue

                    tag_list = _normalize_tags(tags)
                    if has_reject_review_marker(tag_list):
                        continue
                    resolved = resolve_codes_for_tags_cached(tag_list, canon_version, alias_to_canon, key_to_canon)
                    if not resolved:
                        continue

                    val = (text or "").strip()
                    upd = parse_dt_utc(updated)

                    for canonical_code, code_ver in resolved:
                        if code_filter and canonical_code != code_filter:
                            continue
                        if version != "all" and code_ver != version:
                            continue

                        source_label = "gold" if group_role == "gold" else "human"
                        key = (doc_id, canonical_code, source_label)
                        rec = human_agg.get(key)
                        if not rec:
                            rec = {
                                "source": source_label,
                                "code_version": code_ver,
                                "n_annotations": 0,
                                "has_span": False,
                                "span_examples": [],
                                "values_set": set(),
                                "latest_value": None,
                                "latest_updated": None,
                                "annotators": set(),
                            }
                            human_agg[key] = rec

                        rec["n_annotations"] += 1
                        if user_:
                            rec["annotators"].add(user_)

                        if exact and str(exact).strip():
                            rec["has_span"] = True
                            ex = str(exact).strip()
                            if ex and ex not in rec["span_examples"] and len(rec["span_examples"]) < 3:
                                rec["span_examples"].append(ex)

                        if val:
                            rec["values_set"].add(val)
                            if upd and (rec["latest_updated"] is None or upd > rec["latest_updated"]):
                                rec["latest_updated"] = upd
                                rec["latest_value"] = val
                            elif rec["latest_value"] is None:
                                rec["latest_value"] = val

            # -------------------------
            # Model aggregates
            # -------------------------
            model_agg: dict[tuple[str, str], dict] = {}

            if source in {"reviewed", "model", "all"}:
                stmt_model = (
                    select(
                        HypothesisAnnotation.document_id,
                        HypothesisAnnotation.tags,
                        HypothesisAnnotation.text,
                        HypothesisAnnotation.updated,
                    )
                    .join(HypothesisGroup, HypothesisGroup.group_id == HypothesisAnnotation.group_id)
                    .where(HypothesisAnnotation.document_id.in_(doc_ids))
                    .where(HypothesisGroup.group_role == "model")
                    .where(HypothesisAnnotation.source_type == "model")
                )
                for doc_id, tags, text, updated in db.execute(stmt_model).yield_per(5000):
                    if not doc_id:
                        continue
                    tag_list = _normalize_tags(tags)
                    resolved = resolve_codes_for_tags_cached(tag_list, canon_version, alias_to_canon, key_to_canon)
                    if not resolved:
                        continue
                    val = (text or "").strip()
                    upd = parse_dt_utc(updated)
                    for canonical_code, code_ver in resolved:
                        if code_filter and canonical_code != code_filter:
                            continue
                        if version != "all" and code_ver != version:
                            continue
                        key = (doc_id, canonical_code)
                        rec = model_agg.get(key)
                        if not rec:
                            rec = {
                                "code_version": code_ver,
                                "values_set": set(),
                                "latest_value": None,
                                "latest_updated": None,
                            }
                            model_agg[key] = rec
                        if val:
                            rec["values_set"].add(val)
                            if upd and (rec["latest_updated"] is None or upd > rec["latest_updated"]):
                                rec["latest_updated"] = upd
                                rec["latest_value"] = val
                            elif rec["latest_value"] is None:
                                rec["latest_value"] = val

                solr_docs = fetch_model_export_docs(core, doc_ids)
                solr_docs = [
                    d for d in solr_docs
                    if (d.get("code_value_model_norm_kv_ss") or d.get("code_value_model_kv_ss"))
                ]

                solr_model_agg = build_model_long_aggregates(
                    solr_docs,
                    code_filter=(None if code_filter in {None, "__NO_MATCH__"} else code_filter),
                )
                for key, rec in solr_model_agg.items():
                    model_agg.setdefault(key, rec)

            if source == "reviewed":
                rejected = collect_rejected_review_codes(
                    db,
                    doc_ids,
                    uid,
                    canon_version,
                    alias_to_canon,
                    key_to_canon,
                    version=version,
                    code_filter=(None if code_filter in {None, "__NO_MATCH__"} else code_filter),
                )
                final_keys = (
                    {(doc_id, canonical_code) for (doc_id, canonical_code, _source_label) in human_agg.keys()}
                    | set(model_agg.keys())
                )

                for doc_id, canonical_code in sorted(final_keys):
                    human_rec = human_agg.get((doc_id, canonical_code, "human"))
                    gold_rec = human_agg.get((doc_id, canonical_code, "gold"))
                    model_rec = model_agg.get((doc_id, canonical_code))

                    source_label = ""
                    value_mode = ""
                    rec = None
                    if human_rec:
                        rec = human_rec
                        source_label = "human"
                        value_mode = "latest_nonempty_text"
                    elif gold_rec:
                        rec = gold_rec
                        source_label = "gold"
                        value_mode = "gold_reference"
                    elif (doc_id, canonical_code) not in rejected and model_rec:
                        rec = model_rec
                        source_label = "model_implicit_accept"
                        value_mode = "implicit_accept_unchanged_model"

                    if not rec:
                        continue

                    values_list = sorted(list(rec["values_set"]))
                    row = {
                        "project_id": str(project_id),
                        "document_id": doc_id,
                        "canonical_url": doc_url.get(doc_id) or "",
                        "code": canonical_code,
                        "code_version": rec.get("code_version") or "unknown",
                        "source": source_label,
                        "value": rec.get("latest_value") or "",
                        "value_mode": value_mode,
                        "values": json.dumps(values_list, ensure_ascii=False),
                        "n_values": len(values_list),
                        "has_span": bool(rec.get("has_span")) if source_label != "model_implicit_accept" else False,
                        "span_examples": " || ".join(rec.get("span_examples") or []) if source_label != "model_implicit_accept" else "",
                        "n_annotations": rec.get("n_annotations", 0) if source_label != "model_implicit_accept" else 0,
                        "latest_updated": iso_z(rec.get("latest_updated")),
                    }
                    if include_annotators:
                        row["annotators"] = ";".join(sorted(rec.get("annotators") or [])) if source_label != "model_implicit_accept" else ""

                    w.writerow(row)
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate(0)
                return

            # -------------------------
            # Emit human rows
            # -------------------------
            if source in {"human", "gold", "all"}:
                for (doc_id, canonical_code, source_label) in sorted(human_agg.keys()):
                    rec = human_agg[(doc_id, canonical_code, source_label)]
                    values_list = sorted(list(rec["values_set"]))

                    row = {
                        "project_id": str(project_id),
                        "document_id": doc_id,
                        "canonical_url": doc_url.get(doc_id) or "",
                        "code": canonical_code,
                        "code_version": rec["code_version"],
                        "source": rec["source"],
                        "value": rec["latest_value"] or "",
                        "value_mode": "latest_nonempty_text",
                        "values": json.dumps(values_list, ensure_ascii=False),
                        "n_values": len(values_list),
                        "has_span": bool(rec["has_span"]),
                        "span_examples": " || ".join(rec["span_examples"]) if rec["span_examples"] else "",
                        "n_annotations": rec["n_annotations"],
                        "latest_updated": iso_z(rec["latest_updated"]),
                    }
                    if include_annotators:
                        row["annotators"] = ";".join(sorted(rec["annotators"])) if rec["annotators"] else ""

                    w.writerow(row)
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate(0)

            # -------------------------
            # Emit model rows
            # -------------------------
            if source in {"model", "all"}:
                for (doc_id, canonical_code) in sorted(model_agg.keys()):
                    rec = model_agg[(doc_id, canonical_code)]
                    values_list = sorted(list(rec["values_set"]))

                    row = {
                        "project_id": str(project_id),
                        "document_id": doc_id,
                        "canonical_url": doc_url.get(doc_id) or "",
                        "code": canonical_code,
                        "code_version": rec["code_version"],
                        "source": "model",
                        "value": rec["latest_value"] or "",
                        "value_mode": "solr_model_value",
                        "values": json.dumps(values_list, ensure_ascii=False),
                        "n_values": len(values_list),
                        "has_span": False,
                        "span_examples": "",
                        "n_annotations": 0,
                        "latest_updated": "",
                    }
                    if include_annotators:
                        row["annotators"] = ""

                    w.writerow(row)
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate(0)

        filename = f"export_project_{project_id}.csv"
        return StreamingResponse(
            gen(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    finally:
        db.close()

@app.get("/export/csv_wide")
def export_csv_wide(
    request: Request,
    project_id: UUID,
    core: str = "hitl_test",
    document_id: Optional[str] = None,
    document_ids: Optional[str] = None,
    code: Optional[str] = None,
    version: str = "all",
    source: str = "all",
    metric: str = "value",
    column_order: str = "project_document_url",
):
    if version not in {"v1", "ext", "all"}:
        raise HTTPException(400, "version must be v1|ext|all")
    if source not in {"reviewed", "human", "gold", "model", "all"}:
        raise HTTPException(400, "source must be reviewed|human|gold|model|all")
    if metric not in {"value", "count", "binary"}:
        raise HTTPException(400, "metric must be value|count|binary")

    allowed_column_orders = {
        "project_document_url": ["project_id", "document_id", "canonical_url"],
        "document_project_url": ["document_id", "project_id", "canonical_url"],
        "document_url_project": ["document_id", "canonical_url", "project_id"],
        "url_document_project": ["canonical_url", "document_id", "project_id"],
    }
    if column_order not in allowed_column_orders:
        raise HTTPException(400, "column_order is invalid")

    uid = current_user_id(request)

    db = SessionLocal()
    try:
        assert_project_member(db, project_id, uid)

        doc_ids = iter_project_document_ids(
            db,
            str(project_id),
            document_id=document_id,
            document_ids=document_ids,
        )
        doc_ids = list(doc_ids) if doc_ids else []
        if not doc_ids:
            raise HTTPException(404, "No documents matched")

        doc_rows = db.execute(
            select(Document.document_id, Document.canonical_url).where(Document.document_id.in_(doc_ids))
        ).all()
        doc_url = {d: u for (d, u) in doc_rows}

        canon_version, alias_to_canon, key_to_canon = load_code_maps(db)

        code_filter: Optional[str] = None
        if code:
            cf = resolve_tag_to_canonical(code, canon_version, alias_to_canon, key_to_canon)
            code_filter = cf or "__NO_MATCH__"

        # Human/gold aggregates
        per_doc: dict[str, dict[str, dict]] = {}
        codes_seen: set[str] = set()
        gold_per_doc: dict[str, dict[str, dict]] = {}
        gold_codes_seen: set[str] = set()

        if source in {"reviewed", "human", "all"} and code_filter != "__NO_MATCH__":
            per_doc, codes_seen = build_wide_aggregates(
                db,
                doc_ids,
                canon_version,
                alias_to_canon,
                key_to_canon,
                version=version,
                code_filter=(None if code_filter is None else code_filter),
                source_filter="human",
            )

        if source in {"reviewed", "gold", "all"} and code_filter != "__NO_MATCH__":
            gold_per_doc, gold_codes_seen = build_wide_aggregates(
                db,
                doc_ids,
                canon_version,
                alias_to_canon,
                key_to_canon,
                version=version,
                code_filter=(None if code_filter is None else code_filter),
                source_filter="gold",
            )

        if code_filter == "__NO_MATCH__":
            codes_seen = set()
            gold_codes_seen = set()

        # Model aggregates
        model_per_doc: dict[str, dict[str, dict]] = {}
        model_codes_seen: set[str] = set()

        if source in {"reviewed", "model", "all"} and code_filter != "__NO_MATCH__":
            stmt_model = (
                select(
                    HypothesisAnnotation.document_id,
                    HypothesisAnnotation.tags,
                    HypothesisAnnotation.text,
                    HypothesisAnnotation.updated,
                )
                .join(HypothesisGroup, HypothesisGroup.group_id == HypothesisAnnotation.group_id)
                .where(HypothesisAnnotation.document_id.in_(doc_ids))
                .where(HypothesisGroup.group_role == "model")
                .where(HypothesisAnnotation.source_type == "model")
            )
            for doc_id, tags, text, updated in db.execute(stmt_model).yield_per(5000):
                if not doc_id:
                    continue
                tag_list = _normalize_tags(tags)
                resolved = resolve_codes_for_tags_cached(tag_list, canon_version, alias_to_canon, key_to_canon)
                if not resolved:
                    continue
                val = (text or "").strip()
                upd = parse_dt_utc(updated)
                bucket = model_per_doc.setdefault(doc_id, {})
                for canonical_code, code_ver in resolved:
                    if code_filter and canonical_code != code_filter:
                        continue
                    if version != "all" and code_ver != version:
                        continue
                    model_codes_seen.add(canonical_code)
                    rec = bucket.get(canonical_code)
                    if not rec:
                        rec = {"count": 0, "latest_value": None, "latest_updated": None}
                        bucket[canonical_code] = rec
                    rec["count"] += 1
                    if val:
                        if upd and (rec["latest_updated"] is None or upd > rec["latest_updated"]):
                            rec["latest_updated"] = upd
                            rec["latest_value"] = val
                        elif rec["latest_value"] is None:
                            rec["latest_value"] = val

            solr_docs = fetch_model_export_docs(core, doc_ids)
            solr_docs = [
                d for d in solr_docs
                if (d.get("code_value_model_norm_kv_ss") or d.get("code_value_model_kv_ss"))
            ]

            solr_model_per_doc, solr_model_codes_seen = build_model_wide_aggregates(
                solr_docs,
                code_filter=(None if code_filter in {None, "__NO_MATCH__"} else code_filter),
            )
            model_codes_seen.update(solr_model_codes_seen)
            for doc_id_, bucket in solr_model_per_doc.items():
                target = model_per_doc.setdefault(doc_id_, {})
                for canonical_code, rec in bucket.items():
                    target.setdefault(canonical_code, rec)

        base_cols = allowed_column_orders[column_order]

        codes_sorted = sorted(list(codes_seen))
        model_codes_sorted = sorted(list(model_codes_seen))

        if source == "reviewed":
            reviewed_codes_sorted = sorted(list(codes_seen | gold_codes_seen | model_codes_seen))
            code_cols = [csv_safe_col(c) for c in reviewed_codes_sorted]
            rejected_codes = collect_rejected_review_codes(
                db,
                doc_ids,
                uid,
                canon_version,
                alias_to_canon,
                key_to_canon,
                version=version,
                code_filter=(None if code_filter in {None, "__NO_MATCH__"} else code_filter),
            )
        elif source == "human":
            code_cols = [csv_safe_col(c) for c in codes_sorted]
        elif source == "gold":
            code_cols = [csv_safe_col(c) for c in sorted(list(gold_codes_seen))]
        elif source == "model":
            code_cols = [csv_safe_col(c) for c in model_codes_sorted]
        else:
            code_cols = sorted([f"{csv_safe_col(c)}__human" for c in codes_sorted]) + \
                        sorted([f"{csv_safe_col(c)}__gold" for c in gold_codes_seen]) + \
                        sorted([f"{csv_safe_col(c)}__model" for c in model_codes_sorted])

        headers = base_cols + code_cols

        def gen():
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=headers)
            w.writeheader()
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

            for doc_id_ in sorted(doc_ids):
                row = {
                    "project_id": str(project_id),
                    "document_id": doc_id_,
                    "canonical_url": doc_url.get(doc_id_) or "",
                }

                human_bucket = per_doc.get(doc_id_, {})
                model_bucket = model_per_doc.get(doc_id_, {})

                if source == "reviewed":
                    gold_bucket = gold_per_doc.get(doc_id_, {})
                    for canonical_code in reviewed_codes_sorted:
                        col = csv_safe_col(canonical_code)
                        rec = human_bucket.get(canonical_code)
                        if not rec:
                            rec = gold_bucket.get(canonical_code)
                        if not rec and (doc_id_, canonical_code) not in rejected_codes:
                            rec = model_bucket.get(canonical_code)

                        if not rec:
                            row[col] = "" if metric == "value" else 0
                        elif metric == "value":
                            row[col] = rec.get("latest_value") or ""
                        elif metric == "count":
                            row[col] = rec.get("count", 0)
                        else:
                            row[col] = 1

                elif source == "human":
                    for canonical_code in codes_sorted:
                        col = csv_safe_col(canonical_code)
                        rec = human_bucket.get(canonical_code)
                        if not rec:
                            row[col] = "" if metric == "value" else 0
                        elif metric == "value":
                            row[col] = rec.get("latest_value") or ""
                        elif metric == "count":
                            row[col] = rec.get("count", 0)
                        else:
                            row[col] = 1

                elif source == "gold":
                    gold_bucket = gold_per_doc.get(doc_id_, {})
                    for canonical_code in sorted(list(gold_codes_seen)):
                        col = csv_safe_col(canonical_code)
                        rec = gold_bucket.get(canonical_code)
                        if not rec:
                            row[col] = "" if metric == "value" else 0
                        elif metric == "value":
                            row[col] = rec.get("latest_value") or ""
                        elif metric == "count":
                            row[col] = rec.get("count", 0)
                        else:
                            row[col] = 1

                elif source == "model":
                    for canonical_code in model_codes_sorted:
                        col = csv_safe_col(canonical_code)
                        rec = model_bucket.get(canonical_code)
                        if not rec:
                            row[col] = "" if metric == "value" else 0
                        elif metric == "value":
                            row[col] = rec.get("latest_value") or ""
                        elif metric == "count":
                            row[col] = rec.get("count", 0)
                        else:
                            row[col] = 1

                else:  # source == "all"
                    gold_bucket = gold_per_doc.get(doc_id_, {})
                    for canonical_code in codes_sorted:
                        col = f"{csv_safe_col(canonical_code)}__human"
                        rec = human_bucket.get(canonical_code)
                        if not rec:
                            row[col] = "" if metric == "value" else 0
                        elif metric == "value":
                            row[col] = rec.get("latest_value") or ""
                        elif metric == "count":
                            row[col] = rec.get("count", 0)
                        else:
                            row[col] = 1

                    for canonical_code in sorted(list(gold_codes_seen)):
                        col = f"{csv_safe_col(canonical_code)}__gold"
                        rec = gold_bucket.get(canonical_code)
                        if not rec:
                            row[col] = "" if metric == "value" else 0
                        elif metric == "value":
                            row[col] = rec.get("latest_value") or ""
                        elif metric == "count":
                            row[col] = rec.get("count", 0)
                        else:
                            row[col] = 1

                    for canonical_code in model_codes_sorted:
                        col = f"{csv_safe_col(canonical_code)}__model"
                        rec = model_bucket.get(canonical_code)
                        if not rec:
                            row[col] = "" if metric == "value" else 0
                        elif metric == "value":
                            row[col] = rec.get("latest_value") or ""
                        elif metric == "count":
                            row[col] = rec.get("count", 0)
                        else:
                            row[col] = 1

                w.writerow(row)
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

        filename = f"export_wide_project_{str(project_id)}.csv"
        return StreamingResponse(
            gen(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    finally:
        db.close()

###############
# Project and teams apis

# POST /teams (bootstrap teams properly)
# # POST /projects (create project)
# # GET /projects (list projects)
# # GET /projects/{project_id} (basic stats)
# # POST /projects/{project_id}/documents/add (add list of document_ids)
# # GET /projects/{project_id}/documents (paginate membership)
#
# And we will also update Solr membership project_ids_ss via atomic updates (so Solr filters work).

#################################################################################

@app.post("/teams")
def create_team(payload: TeamCreateRequest):
    db = SessionLocal()
    try:
        t = Team(team_id=uuid.uuid4(), name=payload.name.strip())
        db.add(t)
        db.commit()
        return {"ok": True, "team_id": str(t.team_id), "name": t.name}
    finally:
        db.close()



# @app.post("/projects")
# def create_project(payload: ProjectCreateRequest, request: Request):
#     user = get_current_user(request)
#     user_id = user.get("id")
#     if not user_id:
#         raise HTTPException(500, detail="Session user id missing")
#
#     db = SessionLocal()
#     try:
#         team = db.get(Team, payload.team_id)
#         if not team:
#             raise HTTPException(404, "team not found")
#
#         p = Project(
#             project_id=uuid.uuid4(),
#             team_id=payload.team_id,
#             name=payload.name.strip(),
#             description=(payload.description or "").strip() or None,
#         )
#         db.add(p)
#         db.commit()
#         db.refresh(p)
#
#         db.execute(
#             text("""
#                 INSERT INTO project_members (project_id, user_id, role)
#                 VALUES (:pid, :uid, 'owner')
#                 ON CONFLICT (project_id, user_id) DO NOTHING
#             """),
#             {"pid": str(p.project_id), "uid": str(user_id)},
#         )
#         db.commit()
#
#         return {"ok": True, "project_id": str(p.project_id), "team_id": str(p.team_id), "name": p.name, "description": p.description}
#     finally:
#         db.close()


@app.post("/projects")
def create_project(payload: ProjectCreateRequest, request: Request):
    user = get_current_user(request)
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(500, detail="Session user id missing")

    db = SessionLocal()
    try:
        team = db.get(Team, payload.team_id)
        if not team:
            raise HTTPException(404, "team not found")

        name_clean = (payload.name or "").strip()
        desc_clean = (payload.description or "").strip() or None

        if not name_clean:
            raise HTTPException(400, "Project name is required")

        existing = db.execute(
            select(Project)
            .where(Project.team_id == payload.team_id)
            .where(func.lower(Project.name) == name_clean.lower())
        ).scalars().first()

        if existing:
            raise HTTPException(400, "A project with this name already exists for this team")

        p = Project(
            project_id=uuid.uuid4(),
            team_id=payload.team_id,
            name=name_clean,
            description=desc_clean,
        )
        db.add(p)
        db.commit()
        db.refresh(p)

        db.execute(
            text("""
                INSERT INTO project_members (project_id, user_id, role)
                VALUES (:pid, :uid, 'owner')
                ON CONFLICT (project_id, user_id) DO NOTHING
            """),
            {"pid": str(p.project_id), "uid": str(user_id)},
        )
        db.commit()

        return {
            "ok": True,
            "project_id": str(p.project_id),
            "team_id": str(p.team_id),
            "name": p.name,
            "description": p.description,
        }
    finally:
        db.close()

@app.get("/projects")
def list_projects(request: Request, team_id: UUID | None = None):
    user = get_current_user(request)
    user_id = user.get("id")  # ✅ correct
    if not user_id:
        raise HTTPException(500, detail="Session user id missing")

    db = SessionLocal()
    try:
        sql = """
            SELECT p.project_id, p.team_id, p.name, p.description
            FROM projects p
            JOIN project_members pm ON pm.project_id = p.project_id
            WHERE pm.user_id = :uid
        """
        params = {"uid": str(user_id)}

        if team_id:
            sql += " AND p.team_id = :tid"
            params["tid"] = str(team_id)

        sql += " ORDER BY p.name ASC"

        rows = db.execute(text(sql), params).all()

        return {
            "projects": [
                {
                    "project_id": str(r[0]),
                    "team_id": str(r[1]),
                    "name": r[2],
                    "description": r[3],
                }
                for r in rows
            ]
        }
    finally:
        db.close()


# @app.get("/projects/{project_id}")
# def get_project(project_id: UUID, request: Request):
#     uid = current_user_id(request)
#
#     db = SessionLocal()
#     try:
#         assert_project_member(db, project_id, uid)
#
#         p = db.get(Project, project_id)
#         if not p:
#             raise HTTPException(404, "project not found")
#
#         n_docs = db.execute(
#             select(func.count()).select_from(ProjectDocument).where(ProjectDocument.project_id == project_id)
#         ).scalar_one()
#
#         n_docs_with_ann = db.execute(
#             select(func.count(func.distinct(HypothesisAnnotation.document_id)))
#             .select_from(HypothesisAnnotation)
#             .join(ProjectDocument, ProjectDocument.document_id == HypothesisAnnotation.document_id)
#             .where(ProjectDocument.project_id == project_id)
#         ).scalar_one()
#
#         return {
#             "project_id": str(p.project_id),
#             "team_id": str(p.team_id),
#             "name": p.name,
#             "documents_total": int(n_docs),
#             "documents_with_human_annotations": int(n_docs_with_ann),
#         }
#     finally:
#         db.close()


from fastapi import BackgroundTasks
from sqlalchemy.dialects.postgresql import insert as pg_insert

@app.post("/projects/{project_id}/documents/add")
def add_documents_to_project(
    project_id: UUID,
    request: Request,
    payload: ProjectAddDocsRequest,
    background_tasks: BackgroundTasks,   # ✅ ADD
    core: str = "hitl_test",
):
    uid = current_user_id(request)

    doc_ids = [d.strip() for d in (payload.document_ids or []) if d and str(d).strip()]
    seen = set()
    doc_ids = [x for x in doc_ids if not (x in seen or seen.add(x))]
    if not doc_ids:
        raise HTTPException(400, "No document_ids provided")

    db = SessionLocal()
    try:
        assert_project_member(db, project_id, uid)

        p = db.get(Project, project_id)
        if not p:
            raise HTTPException(404, "project not found")

        existing = set(
            db.execute(select(Document.document_id).where(Document.document_id.in_(doc_ids))).scalars().all()
        )
        missing = [d for d in doc_ids if d not in existing]
        if missing:
            raise HTTPException(400, f"{len(missing)} document_ids not found")

        stmt = (
            pg_insert(ProjectDocument)
            .values([{"project_id": project_id, "document_id": did} for did in doc_ids])
            .on_conflict_do_nothing(index_elements=["project_id", "document_id"])
        )
        res = db.execute(stmt)
        db.commit()

        docs_added = int(res.rowcount or 0)

        # ✅ Run Solr update asynchronously so UI returns immediately
        background_tasks.add_task(solr_add_project_membership, core, str(project_id), doc_ids)

        return {
            "ok": True,
            "project_id": str(project_id),
            "docs_added": docs_added,
            "solr_docs_updated": 0,             # will be updated async
            "solr_update_queued": True,         # ✅ tell UI it’s queued
        }
    finally:
        db.close()


# @app.post("/projects/{project_id}/documents/add")
# def add_documents_to_project(
#     project_id: UUID,
#     request: Request,
#     payload: ProjectAddDocsRequest,
#     core: str = "hitl_test",
# ):
#     uid = current_user_id(request)
#
#     db = SessionLocal()
#     try:
#         assert_project_member(db, project_id, uid)
#
#         p = db.get(Project, project_id)
#         if not p:
#             raise HTTPException(404, "project not found")
#
#         existing = set(
#             db.execute(select(Document.document_id).where(Document.document_id.in_(payload.document_ids))).scalars().all()
#         )
#         missing = [d for d in payload.document_ids if d not in existing]
#         if missing:
#             raise HTTPException(400, f"{len(missing)} document_ids not found")
#
#         added = 0
#         for did in payload.document_ids:
#             row = db.get(ProjectDocument, {"project_id": project_id, "document_id": did})
#             if row:
#                 continue
#             db.add(ProjectDocument(project_id=project_id, document_id=did))
#             added += 1
#         db.commit()
#
#         solr_updated = solr_add_project_membership(core, str(project_id), payload.document_ids)
#         return {"ok": True, "project_id": str(project_id), "docs_added": added, "solr_docs_updated": solr_updated}
#     finally:
#         db.close()



# @app.get("/projects/{project_id}/documents")
# def list_project_documents(project_id: UUID, request: Request, limit: int = 50, offset: int = 0):
#     uid = current_user_id(request)
#
#     db = SessionLocal()
#     try:
#         assert_project_member(db, project_id, uid)
#
#         p = db.get(Project, project_id)
#         if not p:
#             raise HTTPException(404, "project not found")
#
#         rows = db.execute(
#             select(ProjectDocument.document_id)
#             .where(ProjectDocument.project_id == project_id)
#             .order_by(ProjectDocument.document_id)
#             .limit(limit)
#             .offset(offset)
#         ).scalars().all()
#
#         return {"project_id": str(project_id), "document_ids": rows, "limit": limit, "offset": offset}
#     finally:
#         db.close()


#################################################################
# SEARCH and SMOKE
#################################################################

from typing import Any, Dict, List, Optional, Union
from fastapi import Query

@app.get("/search")
def search(
    q: str = "*:*",
    core: str = "hitl_test",
    rows: int = 20,
    start: int = 0,
    fq: Optional[List[str]] = Query(None),
    project_id: Optional[str] = None,
    include_hypothesis_links: bool = False,
    group_id: Optional[str] = None,
    # ✅ CRITICAL: accept fl either as "a,b,c" OR as repeated fl=a&fl=b
    fl: Optional[Union[str, List[str]]] = Query(None),
    include_facets: bool = True,
):
    """
    Solr-backed search endpoint.

    Critical fix:
    - Honors caller-provided `fl` (after normalize_fl), so doc_detail can request
      code_value_* fields for the Codes table.
    """

    # -----------------------------
    # Hygiene
    # -----------------------------
    q_clean = (q or "").strip() or "*:*"

    try:
        rows_i = int(rows)
    except Exception:
        rows_i = 20
    try:
        start_i = int(start)
    except Exception:
        start_i = 0

    rows_i = max(1, min(rows_i, 200))
    start_i = max(0, start_i)

    # -----------------------------
    # Fields list (default + override)
    # -----------------------------
    default_fl_list = [
        "document_id_s",
        "canonical_url_s",
        "title_txt",
        "excerpt_txt",
        "published_dt",
        # "doc_type_s",
        # "source_s",
        # "judges_ss",
        "has_human_b",
        "has_any_span_b",
        "has_model_b",
        # "codes_present_human_ss",
        # "codes_present_model_ss",
        "codes_v1_ss",
        "codes_ext_ss",
        "codes_all_ss",
        "project_ids_ss",
        "topics_ss",
        # "topic_keys_ss",
        "topic_kv_ss",
        # "topic_run_id_s",
        "has_topics_b",
        # NOTE: not always needed in search list view, but safe if stored:
        "code_value_human_kv_ss",
        # "code_value_human_norm_kv_ss",
        "code_value_model_kv_ss",
        # "code_value_model_norm_kv_ss",
        # "values_human_txt",
        # "values_human_norm_txt",
        # "values_model_txt",
        # "values_model_norm_txt",
    ]

    fl_norm = normalize_fl(fl)
    fl_out = fl_norm if fl_norm else ",".join(default_fl_list)

    # params: Dict[str, Any] = {
    #     "q": q_clean,
    #     "rows": rows_i,
    #     "start": start_i,
    #     "wt": "json",
    #     "fl": fl_out,  # ✅ this is the critical part
    #     "facet": "true",
    #     "facet.mincount": 1,
    #     "facet.limit": 50,
    #     "facet.field": [
    #         "doc_type_s",
    #         "source_s",
    #         # "judges_ss",
    #         "has_human_b",
    #         "codes_all_ss",
    #         # "appeal_outcome_s",
    #         "topics_ss",
    #     ],
    # }
    params: Dict[str, Any] = {
        "q": q_clean,
        "rows": rows_i,
        "start": start_i,
        "wt": "json",
        "fl": fl_out,
    }
    if include_facets:
        params.update(
            {
                "facet": "true",
                "facet.mincount": 1,
                "facet.limit": -1,
                "facet.field": [
                    "doc_type_s",
                    "source_s",
                    "has_human_b",
                    "codes_all_ss",
                    "codes_present_human_ss",
                    "codes_present_model_ss",
                    # ❌ remove topics_ss (you no longer use Solr topics in UI)
                ],
            }
        )

    # -----------------------------
    # Filters (fq + project scope)
    # -----------------------------
    fq_list: List[str] = []
    has_review_filter = False

    if fq:
        for x in fq:
            if not x:
                continue
            if x.startswith("review_status_by_project_ss:"):
                has_review_filter = True
            fq_list.append(normalize_fq(x))

    if project_id and not has_review_filter:
        fq_list.append(normalize_fq(f'project_ids_ss:"{project_id}"'))

    if fq_list:
        params["fq"] = fq_list

    # -----------------------------
    # Query Solr
    # -----------------------------
    data = solr_select(core, params)

    resp = data.get("response", {}) or {}
    docs = resp.get("docs", []) or []
    facets = (data.get("facet_counts", {}) or {}).get("facet_fields", {}) or {}

    # -----------------------------
    # Normalize docs + add links
    # -----------------------------
    out_docs = []
    for d in docs:
        doc = dict(d)

        cu = doc.get("canonical_url_s")
        if isinstance(cu, list):
            cu = cu[0] if cu else None
        doc["canonical_url_s"] = cu

        if include_hypothesis_links and cu:
            # gid = group_id or "__world__"
            gid = group_id

            if not gid:
                # env override first
                gid = os.getenv("HYPOTHESIS_MODEL_GROUP_ID")

            if not gid:
                # fall back to first enabled group in DB
                db = SessionLocal()
                try:
                    g = db.execute(
                        select(HypothesisGroup)
                        .where(HypothesisGroup.is_enabled == True)
                        .order_by(HypothesisGroup.group_id.asc())
                    ).scalars().first()
                    gid = g.group_id if g else "__world__"
                finally:
                    db.close()

            doc["hypothesis_incontext"] = build_hypothesis_incontext(cu, gid)
            # doc["hypothesis_incontext"] = build_hypothesis_incontext(cu, gid)

        out_docs.append(doc)

    return {
        "ok": True,
        "core": core,
        "q": q_clean,
        "fq": fq_list,
        "numFound": resp.get("numFound", 0),
        "start": start_i,
        "rows": rows_i,
        "docs": out_docs,
        "facets": facets,
        # helpful for debugging:
        "fl": fl_out,
    }




# 3) POST /sample endpoint (random sample via rand_f)
# Why this is the right approach
#
# Because you have rand_f stored and indexed, we can sample by:
#
# generate a random float r in [0,1)
#
# query rand_f:[r TO 1] with base filters → get n docs
#
# if insufficient, wrap: rand_f:[0 TO r) to fill remaining
#
# This is fast, scalable, and doesn’t require expensive random sort.
#
# Payload model

class SampleRequest(BaseModel):
    core: str = "hitl_test"
    q: str = "*:*"
    fq: list[str] = Field(default_factory=list)
    project_id: Optional[UUID] = None
    n: int = 20

@app.post("/sample")
def sample_docs(payload: SampleRequest):
    core = payload.core
    q = payload.q or "*:*"
    n = max(1, min(int(payload.n), 500))  # cap for safety

    # Normalize all incoming fqs (handles review_status_by_project_ss:<uuid>:done)
    fq_list = [normalize_fq(x) for x in (payload.fq or []) if x]

    # Project scoping (Solr membership)
    if payload.project_id:
        fq_list.append(normalize_fq(f"project_ids_ss:{str(payload.project_id)}"))

    fl = ",".join([
        "document_id_s",
        "canonical_url_s",
        "title_txt",
        "excerpt_txt",
        "published_dt",
        "doc_type_s",
        "source_s",
        "judges_ss",
        "has_human_b",
        "has_any_span_b",
        "codes_all_ss",
        "project_ids_ss",
        "rand_f",
    ])

    r = random.random()

    # Also normalize the rand filters (not strictly necessary, but consistent)
    fq_hi = fq_list + [f"rand_f:[{r} TO 1]"]
    fq_lo = fq_list + [f"rand_f:[0 TO {r})"]

    def fetch(fqs: list[str], need: int) -> list[dict]:
        if need <= 0:
            return []
        params = {
            "q": q,
            "fq": fqs,
            "rows": need,
            "start": 0,
            "wt": "json",
            "fl": fl,
        }
        data = solr_select(core, params)
        return (data.get("response", {}) or {}).get("docs", []) or []

    docs = fetch(fq_hi, n)
    if len(docs) < n:
        docs.extend(fetch(fq_lo, n - len(docs)))

    # de-dupe by document_id_s
    seen = set()
    uniq = []
    for d in docs:
        did = d.get("document_id_s")
        if not did or did in seen:
            continue
        seen.add(did)
        uniq.append(d)
        if len(uniq) >= n:
            break

    # normalize canonical_url_s to scalar for consistency with /search
    for d in uniq:
        cu = d.get("canonical_url_s")
        if isinstance(cu, list):
            d["canonical_url_s"] = cu[0] if cu else None

    return {
        "ok": True,
        "core": core,
        "q": q,
        "fq": fq_list,
        "n_requested": n,
        "n_returned": len(uniq),
        "rand_seed": r,
        "docs": uniq,
    }


###################################

def build_hypothesis_incontext(canonical_url: str, group_id: str) -> str:
    """
    Build a Hypothesis in-context link for a document URL and group.
    """
    return (
        "https://hyp.is/go?url="
        + urllib.parse.quote(canonical_url, safe="")
        + "&group="
        + urllib.parse.quote(group_id, safe="")
    )



@app.get("/hypothesis/link")
def hypothesis_link(
    document_id: str,
    group_id: Optional[str] = None,
):
    """
    Returns Hypothesis-friendly links for a document.

    - If group_id is provided, the link will open Hypothesis in that group context.
    - If not provided, it will default to the user's first enabled non-public group (if present),
      else fall back to public.
    """
    db = SessionLocal()
    try:
        doc = db.get(Document, document_id)
        if not doc or not doc.canonical_url:
            raise HTTPException(404, "document not found or has no canonical_url")

        url = doc.canonical_url

        # pick default group if not provided
        gid = group_id
        if not gid:
            g = db.execute(
                select(HypothesisGroup)
                .where(HypothesisGroup.is_enabled == True)
                .order_by(HypothesisGroup.group_id.asc())
            ).scalars().first()
            gid = g.group_id if g else "__world__"

        # Hypothesis "via" incontext link format:
        # https://hyp.is/go?url=<ENCODED_URL>&group=<GROUP_ID>
        incontext = "https://hyp.is/go?url=" + urllib.parse.quote(url, safe="") + "&group=" + urllib.parse.quote(gid, safe="")

        # Direct "annotate" page in Hypothesis client (optional)
        # This usually lands you in the sidebar for that URL in that group
        direct = "https://hypothes.is/?url=" + urllib.parse.quote(url, safe="") + "&group=" + urllib.parse.quote(gid, safe="")

        return {
            "ok": True,
            "document_id": document_id,
            "canonical_url": url,
            "group_id": gid,
            "hypothesis_incontext": incontext,
            "hypothesis_direct": direct,
        }
    finally:
        db.close()


@app.get("/hypothesis/groups")
def list_hypothesis_groups():
    """
    List enabled Hypothesis groups for the current user.
    Robust to minor model-field naming differences.
    """
    db = SessionLocal()
    try:
        rows = (
            db.execute(
                select(HypothesisGroup)
                .where(HypothesisGroup.is_enabled == True)
            )
            .scalars()
            .all()
        )

        groups = []
        for g in rows:
            # tolerate different column names
            gid = getattr(g, "group_id", None) or getattr(g, "id", None)
            name = getattr(g, "name", None) or getattr(g, "group_name", None) or ""
            is_public = getattr(g, "is_public", None)
            if is_public is None:
                is_public = getattr(g, "public", None)
            if is_public is None:
                is_public = False

            groups.append(
                {
                    "group_id": gid,
                    "name": name,
                    "is_public": bool(is_public),
                    "is_enabled": bool(getattr(g, "is_enabled", True)),
                    "group_role": getattr(g, "group_role", None) or "unknown",
                    "owner_user_id": getattr(g, "owner_user_id", None),
                    "is_exportable": bool(getattr(g, "is_exportable", True)),
                    "last_synced_at": (
                        g.last_synced_at.isoformat() if getattr(g, "last_synced_at", None) else None
                    ),
                    "sync_locked_until": (
                        g.sync_locked_until.isoformat() if getattr(g, "sync_locked_until", None) else None
                    ),
                }
            )

        # stable ordering: non-public first, then name, then id
        groups.sort(key=lambda x: (x["is_public"], x["name"], x["group_id"] or ""))

        return {"ok": True, "groups": groups}
    finally:
        db.close()



class ReviewUpdateRequest(BaseModel):
    document_ids: List[str]
    status: str  # unseen|in_progress|done|disputed
    updated_by: Optional[str] = None

@app.post("/projects/{project_id}/review/status")
def set_review_status(
    project_id: UUID,
    request: Request,
    payload: ReviewUpdateRequest,
    core: str = "hitl_test",
):
    status = payload.status.strip().lower()
    if status not in {"unseen", "in_progress", "done", "disputed"}:
        raise HTTPException(status_code=400, detail="invalid status")

    uid = current_user_id(request)
    actor = current_actor(request)

    db = SessionLocal()
    try:
        assert_project_member(db, project_id, uid)

        existing = set(
            db.execute(
                select(ProjectDocument.document_id)
                .where(ProjectDocument.project_id == project_id)
                .where(ProjectDocument.document_id.in_(payload.document_ids))
            ).scalars().all()
        )
        missing = [d for d in payload.document_ids if d not in existing]
        if missing:
            raise HTTPException(
                status_code=400,
                detail={"error": "some document_ids are not in this project", "missing": missing[:20]},
            )

        n = 0
        for did in payload.document_ids:
            row = db.get(ProjectDocumentReview, {"project_id": project_id, "document_id": did})
            if row:
                row.status = status
                row.updated_by = actor
                row.updated_at = datetime.utcnow()
            else:
                db.add(ProjectDocumentReview(
                    project_id=project_id,
                    document_id=did,
                    status=status,
                    updated_by=actor,
                ))
            n += 1

        db.commit()

        solr_updates = [{"document_id_s": did, "project_id": str(project_id), "status": status} for did in payload.document_ids]
        solr_n = solr_update_review_status(core, solr_updates)

        return {"ok": True, "project_id": str(project_id), "status": status, "rows_upserted": n, "solr_docs_updated": solr_n}
    finally:
        db.close()



@app.get("/projects/{project_id}/review/stats")
def project_review_stats(project_id: UUID, request: Request):
    uid = current_user_id(request)

    db = SessionLocal()
    try:
        assert_project_member(db, project_id, uid)

        rows = db.execute(
            select(ProjectDocumentReview.status, func.count())
            .where(ProjectDocumentReview.project_id == project_id)
            .group_by(ProjectDocumentReview.status)
        ).all()

        counts = {status: int(cnt) for status, cnt in rows}
        for k in ["unseen", "in_progress", "done", "disputed"]:
            counts.setdefault(k, 0)

        return {"ok": True, "project_id": str(project_id), "counts": counts}
    finally:
        db.close()



##########################Topic Models end points
@app.post("/topics/runs", response_model=TopicRunOut)
def create_topic_run(payload: TopicRunCreateIn, request: Request):
    """
    USER-ONLY topics:
      - Topic runs are owned by the current user.
      - We do NOT scope runs to projects (project_id is always NULL) to avoid global leakage.
      - If you still want to gate run creation by "currently selected project membership",
        set REQUIRE_PROJECT_MEMBERSHIP_FOR_TOPIC_ACTIONS=1 and pass project_id.
    """
    uid = current_user_id(request)
    actor = current_actor(request)

    db = SessionLocal()
    try:
        require_gate = os.getenv("REQUIRE_PROJECT_MEMBERSHIP_FOR_TOPIC_ACTIONS", "0").strip() == "1"
        if require_gate and payload.project_id:
            assert_project_member(db, payload.project_id, uid)

        row = TopicRun(
            project_id=None,
            name=payload.name.strip(),
            topic_schema_version=(payload.topic_schema_version.strip() or "topics-v1"),
            method=(payload.method or "external").strip(),
            model=(payload.model.strip() if payload.model else None),
            params=payload.params or {},
            is_active=False,
            created_by=str(uid),  # store user_id
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        return TopicRunOut(
            run_id=row.run_id,
            project_id=None,
            name=row.name,
            topic_schema_version=row.topic_schema_version,
            method=row.method,
            model=row.model,
            params=row.params or {},
            is_active=row.is_active,
            created_by=row.created_by,
            created_at=row.created_at,
        )
    finally:
        db.close()




@app.get("/topics/runs")
def list_topic_runs(request: Request, project_id: Optional[UUID] = None):
    uid = current_user_id(request)

    db = SessionLocal()
    try:
        q = select(TopicRun).where(TopicRun.created_by == str(uid))

        if project_id:
            assert_project_member(db, project_id, uid)
            q = q.where(TopicRun.project_id == project_id)
        else:
            # If project_id not provided, keep it safe:
            # return only runs in projects the user is member of
            member_project_ids = db.execute(
                text("SELECT project_id FROM project_members WHERE user_id = :uid"),
                {"uid": str(uid)},
            ).all()
            allowed = [UUID(r[0]) if not isinstance(r[0], UUID) else r[0] for r in member_project_ids]
            if allowed:
                q = q.where(TopicRun.project_id.in_(allowed))
            else:
                return {"ok": True, "runs": []}

        q = q.order_by(TopicRun.created_at.desc())

        rows = db.execute(q).scalars().all()
        out = []
        for r in rows:
            out.append(
                {
                    "run_id": str(r.run_id),
                    "project_id": str(r.project_id) if r.project_id else None,
                    "name": r.name,
                    "topic_schema_version": r.topic_schema_version,
                    "method": r.method,
                    "model": r.model,
                    "params": r.params or {},
                    "is_active": bool(r.is_active),
                    "created_at": iso_z(r.created_at) if getattr(r, "created_at", None) else None,
                }
            )
        return {"ok": True, "runs": out}
    finally:
        db.close()



@app.post("/topics/runs/{run_id}/activate")
def activate_topic_run(run_id: UUID, payload: TopicActivateIn, request: Request):
    """
    USER-ONLY run activation:

    - Personal runs have project_id = NULL (this is normal).
    - Only the owner can activate.
    - Activating a run deactivates the user's other runs (user scope only).
    - No Solr recompute.
    """
    uid = current_user_id(request)
    actor = current_actor(request)

    db = SessionLocal()
    try:
        run = db.get(TopicRun, run_id)
        if not run:
            raise HTTPException(404, "topic run not found")

        # Enforce ownership (user-only)
        assert_topic_run_owner(db, run_id, uid, actor)

        # Deactivate other runs for SAME user (no project scoping)
        others = (
            db.execute(
                select(TopicRun)
                .where(TopicRun.created_by == str(uid))
                .where(TopicRun.run_id != run_id)
            )
            .scalars()
            .all()
        )
        for o in others:
            o.is_active = False

        run.is_active = True
        db.commit()

        # No Solr recompute in user-only mode
        solr_updated = 0

        return {
            "ok": True,
            "run_id": str(run_id),
            "project_id": str(run.project_id) if run.project_id else None,
            "is_active": True,
            "pushed_to_solr": False,
            "solr_docs_updated": solr_updated,
        }
    finally:
        db.close()




@app.post("/topics/ingest")
def ingest_topics(payload: TopicsIngestIn, core: str = "hitl_test"):
    """
    Bulk upsert document topic assignments into Postgres.
    Optionally push derived fields into Solr for filtering/faceting.
    """
    db = SessionLocal()
    try:
        run = db.get(TopicRun, payload.run_id)
        if not run:
            raise HTTPException(404, "topic run not found")

        # validate docs exist (fast safety)
        doc_ids = [it.document_id for it in payload.items if it.document_id]
        existing = set(
            db.execute(select(Document.document_id).where(Document.document_id.in_(doc_ids))).scalars().all()
        )
        missing = [d for d in doc_ids if d not in existing]
        if missing:
            raise HTTPException(400, {"error": "some document_ids not found", "missing": missing[:20]})

        upserted = 0

        # naive upsert (works fine at moderate scale)
        # If you need big scale, we can switch to psycopg COPY or SQL upsert.
        for item in payload.items:
            did = item.document_id.strip()
            for t in item.topics:
                key = (t.topic_key or "").strip()
                lab = (t.topic_label or "").strip()
                if not did or not key or not lab:
                    continue

                row = db.get(DocumentTopic, {"run_id": payload.run_id, "document_id": did, "topic_key": key})

                # HARD BLOCK
                if row and row.status in ("rejected", "deleted"):
                    continue

                # HUMAN PROTECT
                if row and row.assignment_type == "human" and row.status == "active":
                    continue

                if row:
                    row.topic_label = lab
                    row.score = t.score
                    row.source = (t.source or "model").strip()
                    row.evidence = t.evidence or {}
                    row.assignment_type = "auto"
                    row.status = "active"
                    row.reason = "bulk_ingest"
                else:
                    db.add(DocumentTopic(
                        run_id=payload.run_id,
                        document_id=did,
                        topic_key=key,
                        topic_label=lab,
                        score=t.score,
                        source=(t.source or "model").strip(),
                        evidence=t.evidence or {},
                        assignment_type="auto",
                        status="active",
                        reason="bulk_ingest",
                    ))

                upserted += 1

        db.commit()

        solr_updated = 0
        if payload.update_solr:
            batch_doc_ids = [it.document_id.strip() for it in payload.items if it.document_id]

            rows = db.execute(
                select(
                    DocumentTopic.document_id,
                    DocumentTopic.topic_key,
                    DocumentTopic.topic_label,
                    DocumentTopic.score,
                )
                .where(DocumentTopic.run_id == payload.run_id)
                .where(DocumentTopic.document_id.in_(batch_doc_ids))
                .where(DocumentTopic.status == "active")
            ).all()

            doc_to_topics: Dict[str, List[Dict[str, Any]]] = {}
            for did, key, lab, score in rows:
                doc_to_topics.setdefault(did, []).append(
                    {"topic_key": key, "topic_label": lab, "score": score}
                )

            for did in batch_doc_ids:
                doc_to_topics.setdefault(did, [])


            schema_v = run.topic_schema_version if payload.add_schema_version else None
            solr_updated = solr_update_topics_for_docs(
                core=core,
                doc_to_topics=doc_to_topics,
                run_id=str(payload.run_id),
                schema_version=schema_v,
            )

        return {
            "ok": True,
            "run_id": str(payload.run_id),
            "topics_rows_upserted": upserted,
            "solr_docs_updated": solr_updated,
        }
    finally:
        db.close()



# def _recompute_topics_to_solr(db, *, core: str, run_id: UUID, project_id: Optional[UUID] = None) -> int:
#     run = db.get(TopicRun, run_id)
#     if not run:
#         raise HTTPException(404, "topic run not found")
#
#     # doc scope: if project_id specified, intersect with project documents
#     doc_scope: Optional[set[str]] = None
#     if project_id:
#         doc_scope = set(
#             db.execute(select(ProjectDocument.document_id).where(ProjectDocument.project_id == project_id)).scalars().all()
#         )
#
#     stmt = select(
#         DocumentTopic.document_id,
#         DocumentTopic.topic_key,
#         DocumentTopic.topic_label,
#         DocumentTopic.score,
#     ).where(DocumentTopic.run_id == run_id)
#
#     doc_to_topics: Dict[str, List[Dict[str, Any]]] = {}
#     scanned = 0
#
#     for did, tkey, tlab, score in db.execute(stmt).yield_per(5000):
#         scanned += 1
#         if not did:
#             continue
#         if doc_scope is not None and did not in doc_scope:
#             continue
#         bucket = doc_to_topics.setdefault(str(did), [])
#         bucket.append({"topic_key": tkey, "topic_label": tlab, "score": score})
#
#     schema_v = run.topic_schema_version
#     return solr_update_topics_for_docs(
#         core=core,
#         doc_to_topics=doc_to_topics,
#         run_id=str(run_id),
#         schema_version=schema_v,
#     )
def _recompute_topics_to_solr(db, *, core: str, run_id: UUID, project_id: Optional[UUID] = None) -> int:
    """
    USER-SCOPED TOPICS (PRIVATE)

    Recompute-to-Solr is disabled for the same reason as _push_topics_for_docs:
    Solr is shared and would make private topics appear global.

    We keep this function for API compatibility but it is a no-op.
    """
    return 0


@app.post("/solr/recompute_topics")
def recompute_solr_topics(
    core: str = "hitl_test",
    run_id: UUID = None,
    project_id: Optional[UUID] = None,
):
    """
    Recompute Solr topics_* purely from Postgres document_topics for a given run_id.
    Optional project_id scopes to docs in that project.
    """
    if not run_id:
        raise HTTPException(400, "run_id is required")

    db = SessionLocal()
    try:
        updated = _recompute_topics_to_solr(db, core=core, run_id=run_id, project_id=project_id)
        return {
            "ok": True,
            "core": core,
            "run_id": str(run_id),
            "project_id": str(project_id) if project_id else None,
            "docs_updated_in_solr": updated,
        }
    finally:
        db.close()




@app.get("/documents/{document_id}/topics")
def get_document_topics(document_id: str, run_id: UUID):
    db = SessionLocal()
    try:
        run = db.get(TopicRun, run_id)
        if not run:
            raise HTTPException(404, "topic run not found")

        rows = db.execute(
            select(
                DocumentTopic.topic_key,
                DocumentTopic.topic_label,
                DocumentTopic.score,
                DocumentTopic.source,
                DocumentTopic.assignment_type,
                DocumentTopic.status,
                DocumentTopic.evidence,
                DocumentTopic.updated_at,
            )
            .where(DocumentTopic.run_id == run_id)
            .where(DocumentTopic.document_id == document_id)
            .order_by(
                DocumentTopic.score.desc().nullslast(),
                DocumentTopic.topic_key.asc(),
            )
        ).all()

        return {
            "ok": True,
            "run_id": str(run_id),
            "document_id": document_id,
            "topics": [
                {
                    "topic_key": k,
                    "topic_label": lab,
                    "score": sc,
                    "source": src,
                    "assignment_type": atype,
                    "status": status,
                    "evidence": ev or {},
                    "updated_at": iso_z(u) if u else None,
                }
                for (k, lab, sc, src, atype, status, ev, u) in rows
            ],
        }
    finally:
        db.close()


################################ HITL topics modelling #########

class TopicHumanLabelIn(BaseModel):
    # USER-only topics (project_id is optional and used only as an optional permission gate)
    project_id: Optional[UUID] = None
    run_id: UUID | None = None
    document_id: str
    topic_label: str
    topic_key: Optional[str] = None  # optional
    user: Optional[str] = None



class TopicHumanDeleteIn(BaseModel):
    run_id: UUID
    document_id: str
    topic_key: str
    user: Optional[str] = None



class TopicSuggestOut(BaseModel):
    document_id: str
    score: float

# E2) Helpers: embeddings decode + centroid
import numpy as np
from .faiss_store import search_centroid

# EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2").strip()
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2").strip()
HARD_BLOCK_STATUSES = {"rejected", "deleted"}
MIN_HUMAN_LABELS_FOR_PROP = 1


def _bytes_to_vec(b: bytes, dim: int) -> np.ndarray:
    v = np.frombuffer(b, dtype=np.float32)
    if v.shape[0] != dim:
        raise ValueError(f"embedding dim mismatch: {v.shape[0]} vs {dim}")
    return v

def _l2_normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n <= 0:
        return v
    return v / n

def _topic_centroid_from_humans(db, run_id: UUID, topic_key: str) -> Optional[np.ndarray]:
    """
    Returns L2-normalized centroid vector or None.

    Counts only HUMAN+ACTIVE labels that have embeddings.
    Enforces MIN_HUMAN_LABELS_FOR_PROP on number of valid embedding vectors.
    """
    human_docs = db.execute(
        select(DocumentTopic.document_id)
        .where(DocumentTopic.run_id == run_id)
        .where(DocumentTopic.topic_key == topic_key)
        .where(DocumentTopic.assignment_type == "human")
        .where(DocumentTopic.status == "active")
    ).scalars().all()

    if not human_docs:
        return None

    rows = db.execute(
        select(DocEmbedding.embedding_dim, DocEmbedding.embedding)
        .where(DocEmbedding.document_id.in_(human_docs))
        .where(DocEmbedding.model == EMBEDDING_MODEL)
    ).all()

    if not rows:
        return None

    dim = rows[0][0]
    vecs: list[np.ndarray] = []
    for d, b in rows:
        if d != dim or b is None:
            continue
        v = _bytes_to_vec(b, dim)
        if v is None:
            continue
        vecs.append(v)

    if len(vecs) < MIN_HUMAN_LABELS_FOR_PROP:
        return None

    centroid = np.mean(np.stack(vecs, axis=0), axis=0).astype(np.float32)
    return _l2_normalize(centroid)




def _is_hard_blocked(decisions: list[tuple[str, str]]) -> bool:
    """
    decisions: list of (assignment_type, status) for a given (run_id, doc_id, topic_key)
    Hard block means: user explicitly said 'never add again' unless they add a new HUMAN label.
    """
    return any(st in HARD_BLOCK_STATUSES for (_atype, st) in (decisions or []))



# def _propagate_topic(
#     db,
#     *,
#     core: str,
#     run_id: UUID,
#     topic_key: str,
#     topic_label: str,
#     k: int = 200,
#     min_score: float = 0.35
# ) -> Dict[str, Any]:
#     """
#     1) centroid from human labels
#     2) FAISS search
#     3) upsert auto labels (active) excluding:
#          - human active rows
#          - any hard-blocked rows (rejected/deleted)
#     4) push to Solr for affected docs
#     """
#     centroid = _topic_centroid_from_humans(db, run_id, topic_key)
#     if centroid is None:
#         return {
#             "ok": True,
#             "propagated": 0,
#             "updated_doc_ids": [],
#             "reason": f"no centroid (need >= {MIN_HUMAN_LABELS_FOR_PROP} active human labels with embeddings)"
#         }
#
#     hits = search_centroid(centroid, k=k)
#
#     existing_rows = db.execute(
#         select(DocumentTopic.document_id, DocumentTopic.assignment_type, DocumentTopic.status)
#         .where(DocumentTopic.run_id == run_id)
#         .where(DocumentTopic.topic_key == topic_key)
#     ).all()
#
#     existing: dict[str, list[tuple[str, str]]] = {}
#     for did, atype, st in existing_rows:
#         existing.setdefault(did, []).append((atype, st))
#
#     to_upsert: list[tuple[str, float]] = []
#     for did, score in hits:
#         if score is None:
#             continue
#         score = float(score)
#         if score < float(min_score):
#             continue
#
#         decisions = existing.get(did, [])
#
#         # keep human labels untouched
#         if any(t == "human" and s == "active" for (t, s) in decisions):
#             continue
#
#         # block if user rejected/deleted previously (any assignment type)
#         if _is_hard_blocked(decisions):
#             continue
#
#         to_upsert.append((did, score))
#
#     updated_doc_ids: list[str] = []
#     upserted = 0
#
#     for did, score in to_upsert:
#         row = db.execute(
#             select(DocumentTopic)
#             .where(DocumentTopic.run_id == run_id)
#             .where(DocumentTopic.document_id == did)
#             .where(DocumentTopic.topic_key == topic_key)
#             .where(DocumentTopic.assignment_type == "auto")
#         ).scalars().first()
#
#         if row:
#             # extra guard: never resurrect hard-blocked rows
#             if row.status in HARD_BLOCK_STATUSES:
#                 continue
#
#             row.topic_label = topic_label
#             row.score = score
#             row.status = "active"
#             row.reason = "faiss_centroid"
#             row.source = "auto"
#             row.evidence = {
#                 "method": "faiss_centroid",
#                 "embedding_model": EMBEDDING_MODEL,
#                 "score": float(score),
#                 "min_score": float(min_score),
#                 "k": int(k),
#                 "topic_key": topic_key,
#             }
#         else:
#             db.add(DocumentTopic(
#                 run_id=run_id,
#                 document_id=did,
#                 topic_key=topic_key,
#                 topic_label=topic_label,
#                 score=score,
#                 source="auto",
#                 evidence={
#                     "method": "faiss_centroid",
#                     "embedding_model": EMBEDDING_MODEL,
#                     "score": float(score),
#                     "min_score": float(min_score),
#                     "k": int(k),
#                     "topic_key": topic_key,
#                 },
#                 assignment_type="auto",
#                 status="active",
#                 reason="propagation",
#             ))
#
#         updated_doc_ids.append(did)
#         upserted += 1
#
#     db.commit()
#
#     solr_updated = 0
#     if updated_doc_ids:
#         solr_updated = _push_topics_for_docs(db, core=core, run_id=run_id, document_ids=updated_doc_ids)
#
#     return {
#         "ok": True,
#         "propagated": upserted,
#         "updated_doc_ids": updated_doc_ids,
#         "solr_docs_updated": solr_updated,
#     }

def _propagate_topic(
    db,
    *,
    core: str,
    run_id: UUID,
    topic_key: str,
    topic_label: str,
    k: int = 200,
    min_score: float = 0.85,
) -> Dict[str, Any]:
    """
    USER-ONLY propagation (DB-only):

      1) centroid from human labels (run-scoped)
      2) FAISS search
      3) upsert auto labels (active) excluding:
           - human active rows
           - any hard-blocked rows (rejected/deleted)
      4) NO Solr writes (keeps topics private)
    """
    centroid = _topic_centroid_from_humans(db, run_id, topic_key)
    if centroid is None:
        return {
            "ok": True,
            "propagated": 0,
            "updated_doc_ids": [],
            "solr_docs_updated": 0,
            "reason": f"no centroid (need >= {MIN_HUMAN_LABELS_FOR_PROP} active human labels with embeddings)",
        }

    hits = search_centroid(centroid, k=k)

    existing_rows = db.execute(
        select(DocumentTopic.document_id, DocumentTopic.assignment_type, DocumentTopic.status)
        .where(DocumentTopic.run_id == run_id)
        .where(DocumentTopic.topic_key == topic_key)
    ).all()

    existing: dict[str, list[tuple[str, str]]] = {}
    for did, atype, st in existing_rows:
        existing.setdefault(str(did), []).append((str(atype), str(st)))

    to_upsert: list[tuple[str, float]] = []
    for did, score in hits:
        if did is None or score is None:
            continue
        did = str(did)
        score = float(score)
        if score < float(min_score):
            continue

        decisions = existing.get(did, [])

        # keep human labels untouched
        if any(t == "human" and s == "active" for (t, s) in decisions):
            continue

        # hard block if rejected/deleted exists
        if _is_hard_blocked(decisions):
            continue

        to_upsert.append((did, score))

    upserted = 0
    updated_doc_ids: list[str] = []

    for did, score in to_upsert:
        row = db.execute(
            select(DocumentTopic)
            .where(DocumentTopic.run_id == run_id)
            .where(DocumentTopic.document_id == did)
            .where(DocumentTopic.topic_key == topic_key)
            .where(DocumentTopic.assignment_type == "auto")
        ).scalars().first()

        if row:
            # revive/update existing auto row
            row.topic_label = topic_label
            row.score = score
            row.status = "active"
            row.reason = "propagation"
            row.source = "model"
        else:
            db.add(
                DocumentTopic(
                    run_id=run_id,
                    document_id=did,
                    topic_key=topic_key,
                    topic_label=topic_label,
                    score=score,
                    assignment_type="auto",
                    status="active",
                    reason="propagation",
                    source="model",
                    evidence={"action": "propagate"},
                )
            )

        updated_doc_ids.append(did)
        upserted += 1

    db.commit()

    # IMPORTANT: No Solr writes here (privacy)
    return {
        "ok": True,
        "propagated": upserted,
        "updated_doc_ids": updated_doc_ids,
        "solr_docs_updated": 0,
    }

# E4) Push topics for a set of docs (human+auto active)
# This avoids stale topic values without doing a full run recompute.

# def _push_topics_for_docs(db, *, core: str, run_id: UUID, document_ids: List[str]) -> int:
#     if not document_ids:
#         return 0
#
#     rows = db.execute(
#         select(
#             DocumentTopic.document_id,
#             DocumentTopic.topic_key,
#             DocumentTopic.topic_label,
#             DocumentTopic.score,
#             DocumentTopic.assignment_type,
#         )
#         .where(DocumentTopic.run_id == run_id)
#         .where(DocumentTopic.document_id.in_(document_ids))
#         .where(DocumentTopic.status == "active")
#     ).all()
#
#     doc_to = {}
#     for did, k, lab, sc, atype in rows:
#         doc_to.setdefault(did, []).append({
#             "topic_key": k,
#             "topic_label": lab,
#             "score": float(sc) if sc is not None else 1.0  # human gets 1.0 to avoid None issues
#         })
#
#     # Important: clear docs that now have zero active topics
#     docs_with_topics = set(doc_to.keys())
#     docs_without = [d for d in document_ids if d not in docs_with_topics]
#
#     updated = solr_update_topics_for_docs(
#         core=core,
#         doc_to_topics=doc_to,
#         run_id=str(run_id),
#         schema_version=None,  # optional
#     )
#
#     if docs_without:
#         clears = []
#         for did in docs_without:
#             clears.append({
#                 "document_id_s": did,
#                 "has_topics_b": {"set": False},
#                 "topic_run_id_s": {"set": str(run_id)},
#                 "topics_ss": {"set": []},
#                 "topic_keys_ss": {"set": []},
#                 "topic_kv_ss": {"set": []},
#             })
#         for batch in chunked(clears, 500):
#             solr_atomic_update(core, batch, commit_within_ms=30000)

#             updated += len(batch)
#
#     return updated
def _push_topics_for_docs(db, *, core: str, run_id: UUID, document_ids: List[str]) -> int:
    """
    Push topics into Solr.

    IMPORTANT (multi-tenant safety):
      - If topics are USER-SCOPED, pushing into a shared Solr core will leak topics globally.
      - Default behavior: DO NOT push unless PUSH_USER_TOPICS_TO_SOLR=1.
    """
    if os.getenv("PUSH_USER_TOPICS_TO_SOLR", "0").strip() != "1":
        return 0

    if not document_ids:
        return 0

    rows = db.execute(
        select(
            DocumentTopic.document_id,
            DocumentTopic.topic_key,
            DocumentTopic.topic_label,
            DocumentTopic.score,
            DocumentTopic.assignment_type,
        )
        .where(DocumentTopic.run_id == run_id)
        .where(DocumentTopic.document_id.in_(document_ids))
        .where(DocumentTopic.status == "active")
    ).all()

    doc_to = {}
    for did, k, lab, sc, atype in rows:
        doc_to.setdefault(did, []).append({
            "topic_key": k,
            "topic_label": lab,
            "score": float(sc) if sc is not None else 1.0
        })

    docs_with_topics = set(doc_to.keys())
    docs_without = [d for d in document_ids if d not in docs_with_topics]

    updated = solr_update_topics_for_docs(
        core=core,
        doc_to_topics=doc_to,
        run_id=str(run_id),
        schema_version=None,
    )

    if docs_without:
        clears = []
        for did in docs_without:
            clears.append({
                "document_id_s": did,
                "has_topics_b": {"set": False},
                "topic_run_id_s": {"set": str(run_id)},
                "topics_ss": {"set": []},
                "topic_keys_ss": {"set": []},
                "topic_kv_ss": {"set": []},
            })
        for batch in chunked(clears, 500):
            solr_atomic_update(core, batch, commit_within_ms=30000)

            updated += len(batch)

    return updated



def _topic_key_from_label(label: str) -> str:
    """
    Deterministic key: same label => same key.
    Keeps keys short, URL/solr safe, and stable for propagation.
    """
    raw = (label or "").strip().lower()
    if not raw:
        raw = "topic"

    # readable slug
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    slug = slug[:18] if slug else "topic"

    # short hash for uniqueness
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]

    # final key (no spaces)
    return f"T_{slug}_{h}".upper()


# G) The actual user endpoints
# G1) Human assign → propagate → update Solr


from sqlalchemy.exc import IntegrityError

@app.post("/topics/label")
def topic_human_label(
    payload: TopicHumanLabelIn,
    request: Request,
    core: str = SOLR_GLOBAL_CORE,
    k: int = 200,
    min_score: float = 0.85,
):
    uid = current_user_id(request)
    actor = current_actor(request)

    db = SessionLocal()
    try:
        # Keep this if your UI requires project membership to label docs
        # (safe guard for multi-tenant projects)
        # Optional permission gate: require project membership ONLY if enabled.
        require_gate = os.getenv("REQUIRE_PROJECT_MEMBERSHIP_FOR_TOPIC_ACTIONS", "0").strip() == "1"
        if require_gate and payload.project_id:
            assert_project_member(db, payload.project_id, uid)
        # assert_project_member(db, payload.project_id, uid)

        doc = db.get(Document, payload.document_id)
        if not doc:
            raise HTTPException(400, f"document_id not found: {payload.document_id}")

        topic_label = (payload.topic_label or "").strip()
        if not topic_label:
            raise HTTPException(400, "topic_label is required")

        topic_key = (payload.topic_key or "").strip()
        if not topic_key:
            topic_key = _topic_key_from_label(topic_label)

        # Resolve run_id (USER-ONLY run model)
        if not payload.run_id:
            run = _get_or_create_active_topic_run(db, user_id=uid)
            run_id = run.run_id
        else:
            run_id = UUID(str(payload.run_id))
            assert_topic_run_owner(db, run_id, uid, actor)

        def _apply_row(row: DocumentTopic):
            # Convert ANY existing row (auto/rejected/etc.) into active human
            row.topic_label = topic_label
            row.score = None
            row.assignment_type = "human"
            row.status = "active"
            row.updated_by = actor
            row.reason = "human_label"
            row.source = "human"
            row.evidence = {"action": "label", "user": actor}

        # IMPORTANT: do NOT filter on assignment_type here,
        # because auto suggestions share the same PK (run_id, document_id, topic_key).
        row = (
            db.execute(
                select(DocumentTopic)
                .where(DocumentTopic.run_id == run_id)
                .where(DocumentTopic.document_id == payload.document_id)
                .where(DocumentTopic.topic_key == topic_key)
            )
            .scalars()
            .first()
        )

        if row:
            _apply_row(row)
        else:
            db.add(
                DocumentTopic(
                    run_id=run_id,
                    document_id=payload.document_id,
                    topic_key=topic_key,
                    topic_label=topic_label,
                    score=None,
                    assignment_type="human",
                    status="active",
                    created_by=actor,
                    updated_by=actor,
                    reason="human_label",
                    source="human",
                    evidence={"action": "label", "user": actor},
                )
            )

        try:
            db.commit()
        except IntegrityError:
            # Race-safe: another request inserted the PK first.
            db.rollback()
            row2 = (
                db.execute(
                    select(DocumentTopic)
                    .where(DocumentTopic.run_id == run_id)
                    .where(DocumentTopic.document_id == payload.document_id)
                    .where(DocumentTopic.topic_key == topic_key)
                )
                .scalars()
                .first()
            )
            if not row2:
                raise
            _apply_row(row2)
            db.commit()

        # Propagate suggestions in Postgres (this is your key requirement)
        try:
            propagation = _propagate_topic(
                db,
                core=core,
                run_id=run_id,
                topic_key=topic_key,
                topic_label=topic_label,
                k=k,
                min_score=min_score,
            )
        except Exception as e:
            propagation = {
                "ok": False,
                "propagated": 0,
                "solr_docs_updated": 0,
                "updated_doc_ids": [],
                "reason": str(e),
            }

        # If you still push topics into Solr, keep this.
        # If you want ZERO global leakage, gate it behind an env flag (recommended).
        solr_self = _push_topics_for_docs(db, core=core, run_id=run_id, document_ids=[payload.document_id])

        return {
            "ok": True,
            "run_id": str(run_id),
            "topic_key": topic_key,
            "topic_label": topic_label,
            "human_labeled": True,
            "solr_self_updated": solr_self,
            "propagation": propagation,
        }
    finally:
        db.close()








# G2) Human delete (USER-ONLY, no propagation, no Solr)
@app.delete("/topics/label")
def topic_human_delete(
    payload: TopicHumanDeleteIn,
    request: Request,
    core: str = SOLR_GLOBAL_CORE,
    k: int = 200,
    min_score: float = 0.35,
):
    uid = current_user_id(request)
    actor = current_actor(request)

    db = SessionLocal()
    try:
        run_id = UUID(str(payload.run_id))
        assert_topic_run_owner(db, run_id, uid)

        row = (
            db.execute(
                select(DocumentTopic)
                .where(DocumentTopic.run_id == run_id)
                .where(DocumentTopic.document_id == payload.document_id)
                .where(DocumentTopic.topic_key == payload.topic_key)
                .where(DocumentTopic.assignment_type == "human")
            )
            .scalars()
            .first()
        )

        if not row:
            return {"ok": True, "human_deleted": False, "reason": "no human label existed"}

        row.status = "deleted"
        row.updated_by = actor
        row.reason = "human_deleted"
        row.source = "human"
        row.evidence = {"action": "delete", "user": actor}
        db.commit()

        # Re-propagate to refresh auto suggestions (hard-block prevents re-adding if needed)
        prop = _propagate_topic(
            db,
            core=core,
            run_id=run_id,
            topic_key=payload.topic_key,
            topic_label=row.topic_label,
            k=k,
            min_score=min_score,
        )

        return {
            "ok": True,
            "human_deleted": True,
            "propagation": prop,
            "solr_docs_updated": 0,
        }
    finally:
        db.close()




@app.post("/topics/label/delete")
def topics_label_delete_alias(payload: TopicHumanDeleteIn, request: Request):
    return topic_human_delete(payload, request=request)




# G3) Suggestions-only endpoint (for UI preview)
# Does not write anything.

@app.get("/topics/{run_id}/{topic_key}/suggestions")
def topic_suggestions(
    run_id: UUID,
    topic_key: str,
    request: Request,
    k: int = 200,
    min_score: float = 0.35,
):
    uid = current_user_id(request)

    db = SessionLocal()
    try:
        assert_topic_run_owner(db, run_id, uid)

        any_label = (
            db.execute(
                select(DocumentTopic.topic_label)
                .where(DocumentTopic.run_id == run_id)
                .where(DocumentTopic.topic_key == topic_key)
                .where(DocumentTopic.assignment_type == "human")
                .where(DocumentTopic.status == "active")
            )
            .scalars()
            .first()
        )

        if not any_label:
            raise HTTPException(400, "No active human labels exist for this topic_key yet (cannot build centroid).")

        centroid = _topic_centroid_from_humans(db, run_id, topic_key)
        if centroid is None:
            return {
                "ok": True,
                "propagated": 0,
                "reason": f"no centroid (need >= {MIN_HUMAN_LABELS_FOR_PROP} active human labels with embeddings)",
                "suggestions": [],
            }

        hits = search_centroid(centroid, k=k)
        out = [{"document_id": did, "score": float(sc)} for did, sc in hits if sc is not None and float(sc) >= float(min_score)]
        return {"ok": True, "topic_key": topic_key, "topic_label": any_label, "suggestions": out}
    finally:
        db.close()



        
class TopicRejectIn(BaseModel):
    run_id: UUID
    document_id: str
    topic_key: str

@app.post("/topics/reject")
def topic_reject(payload: TopicRejectIn, request: Request, core: str = "hitl_test"):
    """
    USER-ONLY topics:

    - No project membership requirement.
    - Only the run owner can reject.
    - Reject affects only the user's run (DocumentTopic row status).
    - No Solr updates.
    """
    uid = current_user_id(request)
    actor = current_actor(request)

    db = SessionLocal()
    try:
        assert_topic_run_owner(db, payload.run_id, uid, actor)

        row = (
            db.execute(
                select(DocumentTopic)
                .where(DocumentTopic.run_id == payload.run_id)
                .where(DocumentTopic.document_id == payload.document_id)
                .where(DocumentTopic.topic_key == payload.topic_key)
            )
            .scalars()
            .first()
        )

        if row:
            if row.assignment_type == "human" and row.status == "active":
                raise HTTPException(400, "Cannot reject an active human label. Delete it instead.")
            row.assignment_type = "auto"
            row.status = "rejected"
            row.updated_by = actor
            row.reason = "user_rejected"
            row.source = "human"
            row.evidence = {"action": "reject", "user": actor}
        else:
            db.add(
                DocumentTopic(
                    run_id=payload.run_id,
                    document_id=payload.document_id,
                    topic_key=payload.topic_key,
                    topic_label="(rejected)",
                    score=None,
                    source="human",
                    evidence={"action": "reject", "user": actor},
                    assignment_type="auto",
                    status="rejected",
                    reason="user_rejected",
                    created_by=actor,
                    updated_by=actor,
                )
            )

        db.commit()

        return {"ok": True, "rejected": True}
    finally:
        db.close()




def _get_or_create_active_topic_run(
    db,
    *,
    user_id: str,
) -> TopicRun:
    """
    USER-ONLY topic run resolver (robust).

    Rules:
      - Active run is unique per (created_by=user_id)
      - NO project scoping; project_id is always NULL
      - If multiple active exist (legacy), keep the newest and deactivate the rest
      - If none exists: create one, mark it active
    """
    uid = (str(user_id or "")).strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")

    actives = (
        db.execute(
            select(TopicRun)
            .where(TopicRun.created_by == uid)
            .where(TopicRun.is_active.is_(True))
            .order_by(TopicRun.created_at.desc())
        )
        .scalars()
        .all()
    )

    if actives:
        # ensure only the newest remains active
        keep = actives[0]
        changed = False
        for r in actives[1:]:
            if r.is_active:
                r.is_active = False
                changed = True
        if changed:
            db.commit()
        return keep

    # No active run -> create one
    new_run = TopicRun(
        project_id=None,  # personal run
        name="personal",
        topic_schema_version="topics-v1",
        method="external",
        model=None,
        params={},
        is_active=True,
        created_by=uid,
    )
    db.add(new_run)
    db.commit()
    db.refresh(new_run)
    return new_run






############## Scoping projects and topic modeartion to user only and not global.

def get_current_user(request: Request) -> dict:
    """
    Reads the logged-in user from the session.
    Supports either:
      - request.session["user"] = {"id": "...", ...}
      - request.session["user"] = {"user_id": "...", ...}  (legacy)
    """
    u = request.session.get("user")
    if not u:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Normalize: ensure both keys exist (id + user_id) for compatibility
    if "id" in u and "user_id" not in u:
        u["user_id"] = u["id"]
    if "user_id" in u and "id" not in u:
        u["id"] = u["user_id"]

    return u


def current_user_id(request: Request) -> str:
    u = get_current_user(request)
    uid = (u.get("id") or "").strip()   # ✅ auth router stores "id"
    if not uid:
        raise HTTPException(status_code=500, detail="Session user.id missing")
    return uid

def current_actor(request: Request) -> str:
    u = get_current_user(request)
    actor = (u.get("username") or u.get("email") or u.get("id") or "").strip()
    if not actor:
        raise HTTPException(status_code=500, detail="Session actor missing")
    return actor

def assert_project_member(db, project_id: UUID, user_id: str):
    row = db.execute(
        text("""
            SELECT 1
            FROM project_members
            WHERE project_id = :pid AND user_id = :uid
            LIMIT 1
        """),
        {"pid": str(project_id), "uid": str(user_id)},
    ).first()
    if not row:
        raise HTTPException(status_code=403, detail="Not a member of this project")

def assert_topic_run_owner(db, run_id: UUID, user_id: str, actor: str | None = None):
    run = db.get(TopicRun, run_id)
    if not run:
        raise HTTPException(404, "topic run not found")

    uid = (str(user_id or "")).strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")

    owner = (str(run.created_by or "")).strip()
    if not owner:
        raise HTTPException(status_code=403, detail="Topic run has no owner (created_by is empty)")

    act = (str(actor or "")).strip()

    # Allow:
    #  - exact user_id match (correct modern behavior)
    #  - legacy match against actor string (older rows saved username/email)
    if owner != uid and (not act or owner != act):
        raise HTTPException(status_code=403, detail="Only topic-run creator can modify topics")

    return run



@app.get("/hypothesis/workspace")
def get_my_hypothesis_workspace(request: Request):
    uid = current_user_id(request)  # uses session user.id :contentReference[oaicite:2]{index=2}
    db = SessionLocal()
    try:
        row = db.get(UserHypothesisWorkspace, uid)
        return {"ok": True, "user_id": uid, "group_id": (row.group_id if row else None)}
    finally:
        db.close()


class WorkspaceSetIn(BaseModel):
    group_id: str
    group_name: Optional[str] = None


class ProjectReviewGroupSetIn(BaseModel):
    project_id: UUID
    group_id: str
    group_name: Optional[str] = None


def normalize_hypothesis_group_ref(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme or parsed.netloc:
        parts = [p for p in (parsed.path or "").split("/") if p]
        if len(parts) >= 2 and parts[0] == "groups":
            return parts[1].strip()
        return ""

    raw = raw.strip().strip("/")
    if "/" in raw:
        parts = [p for p in raw.split("/") if p]
        if len(parts) >= 2 and parts[0] == "groups":
            return parts[1].strip()
        return ""
    return raw


@app.get("/hypothesis/project_review_group")
def get_project_hypothesis_review_group(project_id: UUID, request: Request):
    uid = current_user_id(request)
    db = SessionLocal()
    try:
        assert_project_member(db, project_id, uid)
        row = db.get(ProjectHypothesisReviewGroup, project_id)
        if not row:
            return {"ok": True, "project_id": str(project_id), "group_id": None, "group": None}

        group = db.get(HypothesisGroup, row.group_id)
        return {
            "ok": True,
            "project_id": str(project_id),
            "group_id": row.group_id,
            "group": {
                "group_id": row.group_id,
                "name": (group.name if group else "") or row.group_id,
                "is_enabled": bool(group.is_enabled) if group else False,
                "group_role": (group.group_role if group else "") or "project_review",
                "last_synced_at": group.last_synced_at.isoformat() if group and group.last_synced_at else "",
            },
        }
    finally:
        db.close()


@app.post("/hypothesis/project_review_group")
def set_project_hypothesis_review_group(payload: ProjectReviewGroupSetIn, request: Request):
    uid = current_user_id(request)
    gid = normalize_hypothesis_group_ref(payload.group_id)
    if not gid:
        raise HTTPException(status_code=400, detail="group_id is required")
    if gid == HYPOTHESIS_PUBLIC_GROUP_ID:
        raise HTTPException(status_code=400, detail="Choose a private project review group, not Public")

    db = SessionLocal()
    try:
        assert_project_member(db, payload.project_id, uid)

        profile = hypothesis_get_profile()
        server_userid = hypothesis_profile_userid(profile)
        profile_group = next((g for g in (profile.get("groups") or []) if g.get("id") == gid), None)
        server_has_access = bool(profile_group)

        group_name = (payload.group_name or "").strip()
        if profile_group:
            if profile_group.get("public"):
                raise HTTPException(status_code=400, detail="Choose a private project review group, not Public")
            group_name = profile_group.get("name") or group_name or gid

        group = db.get(HypothesisGroup, gid)
        if not group:
            group = HypothesisGroup(
                group_id=gid,
                name=group_name or f"Project review workspace ({gid})",
                organization=(profile_group or {}).get("organization"),
                scopes=(profile_group or {}).get("scopes") or [],
                is_enabled=server_has_access,
                group_role="project_review",
                owner_user_id=None,
                is_exportable=True,
            )
            db.add(group)
        else:
            if group_name:
                group.name = group_name
            if profile_group:
                group.organization = profile_group.get("organization")
                group.scopes = profile_group.get("scopes") or []
                group.is_enabled = True
            group.group_role = "project_review"
            group.is_exportable = True

        row = db.get(ProjectHypothesisReviewGroup, payload.project_id)
        if row:
            row.group_id = gid
            row.created_by = row.created_by or uid
        else:
            db.add(ProjectHypothesisReviewGroup(project_id=payload.project_id, group_id=gid, created_by=uid))

        db.commit()
        return {
            "ok": True,
            "project_id": str(payload.project_id),
            "group_id": gid,
            "server_has_access": server_has_access,
            "server_userid": server_userid,
            "warning": None if server_has_access else (
                f"The server Hypothesis account ({server_userid}) is not a member of this group yet."
            ),
        }
    finally:
        db.close()


@app.post("/hypothesis/workspace")
def set_my_hypothesis_workspace(payload: WorkspaceSetIn, request: Request):
    uid = current_user_id(request)
    gid = normalize_hypothesis_group_ref(payload.group_id)
    if not gid:
        raise HTTPException(status_code=400, detail="group_id is required")
    if gid == HYPOTHESIS_PUBLIC_GROUP_ID:
        raise HTTPException(status_code=400, detail="Choose a private Hypothesis group, not Public")

    db = SessionLocal()
    try:
        g = db.get(HypothesisGroup, gid)
        if not g:
            g = HypothesisGroup(
                group_id=gid,
                name=(payload.group_name or f"Personal workspace ({gid})").strip(),
                scopes=[],
                is_enabled=False,
                group_role="human_workspace",
                owner_user_id=uid,
                is_exportable=True,
            )
            db.add(g)
        elif payload.group_name and not g.name:
            g.name = payload.group_name.strip()

        row = db.get(UserHypothesisWorkspace, uid)
        if row:
            row.group_id = gid
        else:
            db.add(UserHypothesisWorkspace(user_id=uid, group_id=gid))

        db.commit()
        return {"ok": True, "user_id": uid, "group_id": gid}
    finally:
        db.close()


class ProjectUpdateRequest(BaseModel):
    name: str
    description: str | None = None


@app.get("/projects/{project_id}/documents")
def list_project_documents(project_id: UUID, request: Request, limit: int = 50, offset: int = 0):
    uid = current_user_id(request)

    db = SessionLocal()
    try:
        assert_project_member(db, project_id, uid)

        p = db.get(Project, project_id)
        if not p:
            raise HTTPException(404, "project not found")

        rows = (
            db.execute(
                select(
                    Document.document_id,
                    Document.title,
                    Document.published_date,
                )
                .select_from(ProjectDocument)
                .join(Document, Document.document_id == ProjectDocument.document_id)
                .where(ProjectDocument.project_id == project_id)
                .order_by(Document.title.asc(), Document.document_id.asc())
                .limit(limit)
                .offset(offset)
            )
            .all()
        )

        return {
            "project_id": str(project_id),
            "documents": [
                {
                    "document_id": document_id,
                    "title": title,
                    "published_date": str(published_date) if published_date else None,
                }
                for document_id, title, published_date in rows
            ],
            "limit": limit,
            "offset": offset,
        }
    finally:
        db.close()

@app.get("/projects/{project_id}")
def get_project(project_id: UUID, request: Request):
    uid = current_user_id(request)

    db = SessionLocal()
    try:
        assert_project_member(db, project_id, uid)

        p = db.get(Project, project_id)
        if not p:
            raise HTTPException(404, "project not found")

        n_docs = db.execute(
            select(func.count()).select_from(ProjectDocument).where(ProjectDocument.project_id == project_id)
        ).scalar_one()

        n_docs_with_ann = db.execute(
            select(func.count(func.distinct(HypothesisAnnotation.document_id)))
            .select_from(HypothesisAnnotation)
            .join(ProjectDocument, ProjectDocument.document_id == HypothesisAnnotation.document_id)
            .where(ProjectDocument.project_id == project_id)
        ).scalar_one()

        return {
            "project_id": str(p.project_id),
            "team_id": str(p.team_id),
            "name": p.name,
            "description": p.description,
            "documents_total": int(n_docs),
            "documents_with_human_annotations": int(n_docs_with_ann),
        }
    finally:
        db.close()
