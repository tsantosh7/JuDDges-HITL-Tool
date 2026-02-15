# api/app/ui/routes.py
from __future__ import annotations

import logging
import os
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse


from app.auth.deps import require_paid_user, require_role, require_user
from app.db import SessionLocal

from sqlalchemy import select, text, func
from app.models import TopicRun, DocumentTopic


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"])


# ============================================================
# Internal ASGI call helpers (UI -> API) with COOKIE FORWARDING
# ============================================================
def _forward_cookie_headers(request: Request) -> dict:
    """
    Forward browser session cookies into internal ASGI calls so that
    API endpoints protected by session auth work correctly.
    """
    cookie_hdr = request.headers.get("cookie")
    headers: dict = {}
    if cookie_hdr:
        headers["cookie"] = cookie_hdr
    return headers


def _flatten_params(p: dict | None) -> list[tuple[str, str]]:
    """
    Turn dict params into a list of (k,v) pairs.
    If value is list/tuple, encode as repeated params: k=a&k=b.
    """
    if not p:
        return []
    out: list[tuple[str, str]] = []
    for k, v in p.items():
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


async def asgi_get(request: Request, path: str, params: dict | None = None):
    """
    Internal ASGI call helper (UI -> API).

    - Forwards cookies (fixes 401 Not authenticated)
    - Properly encodes list-valued query params
    - Raises a helpful error including response body snippet
    """
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://app") as client:
        flat = _flatten_params(params)
        headers = _forward_cookie_headers(request)
        r = await client.get(path, params=flat, headers=headers)
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            snippet = ""
            try:
                snippet = (r.text or "")[:2000]
            except Exception:
                snippet = ""
            raise httpx.HTTPStatusError(
                message=f"ASGI GET failed: {r.status_code} {path} params={flat} body={snippet}",
                request=e.request,
                response=e.response,
            ) from e

        # allow non-json responses if needed
        ct = (r.headers.get("content-type") or "").lower()
        if ct.startswith("application/json"):
            return r.json()
        return {"text": r.text}


async def asgi_post_json(request: Request, path: str, payload: dict):
    """
    Internal ASGI POST helper that forwards cookies.
    """
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://app") as client:
        headers = _forward_cookie_headers(request)
        r = await client.post(path, json=payload, timeout=60, headers=headers)

    if r.status_code == 422:
        logger.error("422 from %s payload=%s resp=%s", path, payload, r.text)

    r.raise_for_status()
    return r.json() if r.content else None


async def asgi_patch(request: Request, path: str):
    """
    Internal ASGI PATCH helper that forwards cookies.
    """
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://app") as client:
        headers = _forward_cookie_headers(request)
        r = await client.patch(path, headers=headers)
        r.raise_for_status()
        if (r.headers.get("content-type") or "").startswith("application/json"):
            return r.json()
        return {"ok": True}


# ============================================================
# Hypothesis helpers (kept if you still use them elsewhere)
# ============================================================
def build_hypothesis_incontext(url: str, group_id: str = "__world__") -> str:
    return (
        "https://hyp.is/go?url="
        + urllib.parse.quote(url, safe="")
        + "&group="
        + urllib.parse.quote(group_id, safe="")
    )


def build_hypothesis_direct(url: str, group_id: str = "__world__") -> str:
    return (
        "https://hypothes.is/?url="
        + urllib.parse.quote(url, safe="")
        + "&group="
        + urllib.parse.quote(group_id, safe="")
    )


# ============================================================
# Session helpers
# ============================================================
def _get_project_id(request: Request) -> str | None:
    return request.session.get("project_id")


def _set_project_id(request: Request, project_id: str) -> None:
    request.session["project_id"] = project_id


def _get_project_name(request: Request) -> str | None:
    return request.session.get("project_name")


def _set_project_name(request: Request, name: str | None) -> None:
    if name:
        request.session["project_name"] = name
    else:
        request.session.pop("project_name", None)


def _get_run_id(request: Request) -> str | None:
    return request.session.get("topic_run_id")


def _set_run_id(request: Request, run_id: str | None) -> None:
    if run_id:
        request.session["topic_run_id"] = run_id
    else:
        request.session.pop("topic_run_id", None)


async def _ensure_project_selected(request: Request) -> str | None:
    return _get_project_id(request)


async def _pick_run_id_for_project(request: Request, project_id: str) -> str | None:
    runs_res = await asgi_get(request, "/topics/runs", params={"project_id": project_id})
    runs = runs_res.get("runs", []) or []
    if not runs:
        return None
    active = next((r for r in runs if r.get("is_active")), None)
    chosen = active or runs[0]
    return chosen.get("run_id")


# ============================================================
# Solr query helpers
# ============================================================
def _solr_escape_phrase(s: str) -> str:
    """Safe for Solr phrase queries: field:"...". Escapes backslash + quotes."""
    s = (s or "").strip()
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return s


def _solr_escape_query(s: str) -> str:
    """
    Safe-ish escape for edismax query text. We escape common special chars so user
    can type normal text without breaking Solr parser.
    """
    s = (s or "").strip()
    if not s:
        return ""
    for ch in r'+-!():^[]"{}~*?|&\\/':
        s = s.replace(ch, "\\" + ch)
    return s


# def build_user_friendly_q(kw: str | None, kw_field: str, include_codes_topics: bool) -> str:
#     """
#     User-friendly search:
#       - title/excerpt/body full text
#       - human/model extracted values (raw + normalized)
#     Optional:
#       - codes_all_ss, topics_ss, topic_keys_ss, topic_kv_ss
#     """
#     kw = (kw or "").strip()
#     if not kw:
#         return "*:*"
#
#     if kw_field == "url":
#         return f'canonical_url_s:"{_solr_escape_phrase(kw)}"'
#     if kw_field == "id":
#         return f'document_id_s:"{_solr_escape_phrase(kw)}"'
#
#     qtext = _solr_escape_query(kw)
#
#     qf_all = (
#         "title_txt^4 "
#         "excerpt_txt^2 "
#         "body_txt "
#         "values_human_txt "
#         "values_model_txt "
#         "values_human_norm_txt "
#         "values_model_norm_txt"
#     )
#     qf_title = "title_txt^4"
#     qf_excerpt = "excerpt_txt^2"
#     qf_body = "body_txt"
#     qf_values = "values_human_txt values_model_txt values_human_norm_txt values_model_norm_txt"
#
#     if kw_field == "title":
#         qf = qf_title
#     elif kw_field == "excerpt":
#         qf = qf_excerpt
#     elif kw_field == "body":
#         qf = qf_body
#     elif kw_field == "values":
#         qf = qf_values
#     else:
#         qf = qf_all
#
#     base = f'{{!edismax qf="{qf}" pf="{qf_title}" mm=1}}{qtext}'
#
#     if include_codes_topics:
#         p = _solr_escape_phrase(kw)
#         extra = (
#             f' OR codes_all_ss:"{p}"'
#             f' OR topics_ss:"{p}"'
#             f' OR topic_keys_ss:"{p}"'
#             f' OR topic_kv_ss:"{p}"'
#         )
#         return f"({base}{extra})"
#
#     return base

def build_user_friendly_q(kw: str | None, kw_field: str, include_codes_topics: bool) -> str:
    """
    User-friendly search over Solr text + extracted values.

    IMPORTANT:
      - topics are USER-SCOPED (Postgres), so we DO NOT query Solr topic fields here.
      - we keep codes_all_ss optional OR because codes are still Solr-side.
    """
    kw = (kw or "").strip()
    if not kw:
        return "*:*"

    if kw_field == "url":
        return f'canonical_url_s:"{_solr_escape_phrase(kw)}"'
    if kw_field == "id":
        return f'document_id_s:"{_solr_escape_phrase(kw)}"'

    qtext = _solr_escape_query(kw)

    qf_all = (
        "title_txt^4 "
        "excerpt_txt^2 "
        "body_txt "
        "values_human_txt "
        "values_model_txt "
        "values_human_norm_txt "
        "values_model_norm_txt"
    )
    qf_title = "title_txt^4"
    qf_excerpt = "excerpt_txt^2"
    qf_body = "body_txt"
    qf_values = "values_human_txt values_model_txt values_human_norm_txt values_model_norm_txt"

    if kw_field == "title":
        qf = qf_title
    elif kw_field == "excerpt":
        qf = qf_excerpt
    elif kw_field == "body":
        qf = qf_body
    elif kw_field == "values":
        qf = qf_values
    else:
        qf = qf_all

    base = f'{{!edismax qf="{qf}" pf="{qf_title}" mm=1}}{qtext}'

    # ✅ Keep codes OR (optional), but DO NOT include Solr topics fields
    if include_codes_topics:
        p = _solr_escape_phrase(kw)
        extra = f' OR codes_all_ss:"{p}"'
        return f"({base}{extra})"

    return base

# ============================================================
# Root -> Dashboard
# ============================================================
@router.get("/", response_class=HTMLResponse)
def ui_root(request: Request):
    return RedirectResponse("/ui/dashboard", status_code=303)


# ============================================================
# Dashboard
# ============================================================
@router.get("/dashboard", response_class=HTMLResponse)
async def ui_dashboard(request: Request, user=Depends(require_user)):
    # ✅ This now works because asgi_get forwards cookies
    projects_res = await asgi_get(request, "/projects", params={})
    projects = projects_res.get("projects", []) or []

    selected_project_id = _get_project_id(request)
    selected_project_name = _get_project_name(request)

    if not selected_project_id and len(projects) == 1:
        only = projects[0]
        _set_project_id(request, only.get("project_id"))
        _set_project_name(request, only.get("name"))
        selected_project_id = only.get("project_id")
        selected_project_name = only.get("name")

    msg = request.query_params.get("msg")

    # Compute access state (same logic as require_paid_user, but non-blocking)
    role = (user.get("role") or "").lower()
    plan = (user.get("plan") or "free").lower()
    access_until = user.get("access_until")

    access_ok = True
    access_reason = None

    if role != "admin":
        if plan == "free":
            access_ok = False
            access_reason = "Your account is on the free plan. Redeem a code or upgrade to continue."
        elif access_until:
            dt = None
            try:
                dt = datetime.fromisoformat(access_until)
            except Exception:
                dt = None
            if dt and dt <= datetime.now(timezone.utc):
                access_ok = False
                access_reason = "Your access has expired. Redeem a code or upgrade to continue."

    # Collaboration list
    db = SessionLocal()
    try:
        collabs = db.execute(
            text(
                """
                SELECT
                  u.username,
                  u.email,
                  p.name AS project_name,
                  p.description AS project_description
                FROM project_members pm
                JOIN users u ON u.id = pm.user_id
                JOIN projects p ON p.project_id = pm.project_id
                ORDER BY p.name, u.username
                """
            )
        ).mappings().all()
        collabs = [dict(r) for r in collabs]
    finally:
        db.close()

    return request.app.state.templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "projects": projects,
            "has_projects": bool(projects),
            "selected_project_id": selected_project_id,
            "selected_project_name": selected_project_name,
            "message": msg,
            "collabs": collabs,
            "access_ok": access_ok,
            "access_reason": access_reason,
        },
    )


@router.post("/select_project")
async def ui_select_project(
    request: Request,
    project_id: str = Form(...),
    user=Depends(require_paid_user),
):
    proj = await asgi_get(request, f"/projects/{project_id}", params={})
    _set_project_id(request, project_id)
    _set_project_name(request, proj.get("name"))
    _set_run_id(request, None)
    return RedirectResponse("/ui/dashboard", status_code=303)


# ============================================================
# Documentation / About
# ============================================================
@router.get("/documentation", response_class=HTMLResponse)
async def ui_documentation(request: Request, user=Depends(require_paid_user)):
    return request.app.state.templates.TemplateResponse(
        "documentation.html",
        {
            "request": request,
            "user": user,
            "active_nav": "documentation",
            "selected_project_id": _get_project_id(request),
            "selected_project_name": _get_project_name(request),
        },
    )


# ✅ Make About visible on all pages by using require_user (not require_paid_user)
@router.get("/about", response_class=HTMLResponse)
async def ui_about(request: Request, user=Depends(require_user)):
    return request.app.state.templates.TemplateResponse(
        "about.html",
        {
            "request": request,
            "user": user,
            "active_nav": "about",
            "selected_project_id": request.session.get("project_id"),
            "selected_project_name": request.session.get("project_name"),
        },
    )


# ============================================================
# Search Documents
# ============================================================

@router.get("/search", response_class=HTMLResponse)
async def ui_search(
    request: Request,
    q: str = "",
    kw: str | None = None,
    kw_field: str = "all",
    include_codes_topics: str | None = "1",
    start: int = 0,
    scope: str = "all",
    code: str | None = None,
    topic: str | None = None,
    has_human: str | None = None,
    has_any_span: str | None = None,
    user=Depends(require_paid_user),
):
    import os
    import urllib.parse
    from uuid import UUID
    from sqlalchemy import select, func

    core = (request.query_params.get("core") or request.session.get("core") or os.getenv("SOLR_GLOBAL_CORE") or "hitl_test").strip()
    if not core:
        core = "hitl_test"
    request.session["core"] = core

    project_id = _get_project_id(request)
    project_name = _get_project_name(request)

    rows = 20
    try:
        start_i = int(start)
    except Exception:
        start_i = 0
    if start_i < 0:
        start_i = 0

    def _as_bool_choice(v: str | None) -> str | None:
        if v is None:
            return None
        s = str(v).strip().lower()
        if s in ("", "any", "all"):
            return None
        if s in ("1", "true", "yes", "y"):
            return "true"
        if s in ("0", "false", "no", "n"):
            return "false"
        return None

    # If user requests project scope but no project is selected -> bounce
    if scope == "project" and not project_id:
        return RedirectResponse("/ui/dashboard?msg=Please%20select%20a%20project%20first", status_code=303)

    fq: list[str] = []

    # ✅ Code filter stays Solr-side
    if code:
        fq.append(f'codes_all_ss:"{_solr_escape_phrase(code)}"')

    # ❌ DO NOT filter by Solr topics fields (global)
    human_choice = _as_bool_choice(has_human)
    if human_choice == "true":
        fq.append("has_human_b:true")
    elif human_choice == "false":
        fq.append("has_human_b:false")

    span_choice = _as_bool_choice(has_any_span)
    if span_choice == "true":
        fq.append("has_any_span_b:true")
    elif span_choice == "false":
        fq.append("has_any_span_b:false")

    advanced_q = (q or "").strip()
    kw_clean = (kw or "").strip()

    if advanced_q:
        effective_q = advanced_q
    else:
        # IMPORTANT: build_user_friendly_q() must NOT include Solr topic fields (yours is correct)
        effective_q = build_user_friendly_q(
            kw_clean,
            kw_field,
            include_codes_topics=(include_codes_topics == "1" or include_codes_topics is None),
        )

    # ✅ Minimal Solr fields; topics are shown from Postgres, not Solr
    fl = ",".join(
        [
            "document_id_s",
            "title_txt",
            "published_dt",
            "canonical_url_s",
            "has_human_b",
            "has_any_span_b",
        ]
    )

    # ---------------- USER-ONLY RUN RESOLUTION ----------------
    uid = user.get("id") or user.get("user_id") or user.get("sub")
    if not uid:
        raise HTTPException(401, "Not authenticated")

    run_id = _get_run_id(request)
    if not run_id:
        db = SessionLocal()
        try:
            # USER-ONLY active run: project_id is ignored
            r = (
                db.execute(
                    select(TopicRun)
                    .where(TopicRun.created_by == str(uid))
                    .where(TopicRun.is_active.is_(True))
                    .order_by(TopicRun.created_at.desc())
                )
                .scalars()
                .first()
            )
            run_id = str(r.run_id) if r else None
        finally:
            db.close()

        _set_run_id(request, run_id)

    # ✅ Topic filter (user-specific): use Postgres DocumentTopic -> restrict Solr by doc_id list
    if (topic or "").strip() and run_id:
        topic_val = (topic or "").strip()
        db = SessionLocal()
        try:
            doc_ids = (
                db.execute(
                    select(DocumentTopic.document_id)
                    .where(DocumentTopic.run_id == UUID(run_id))
                    .where(DocumentTopic.status == "active")
                    .where((DocumentTopic.topic_label == topic_val) | (DocumentTopic.topic_key == topic_val))
                )
                .scalars()
                .all()
            )
        finally:
            db.close()

        doc_ids = [d for d in doc_ids if d]
        if not doc_ids:
            result = {"ok": True, "docs": [], "numFound": 0, "start": start_i, "facets": {}}
            return request.app.state.templates.TemplateResponse(
                "search.html",
                {
                    "request": request,
                    "user": user,
                    "core": core,
                    "project_id": project_id,
                    "project_name": project_name,
                    "scope": scope,
                    "q": advanced_q,
                    "kw": kw_clean,
                    "kw_field": kw_field,
                    "include_codes_topics": include_codes_topics or "1",
                    "start": start_i,
                    "code": code,
                    "topic": topic,
                    "has_human": has_human,
                    "has_any_span": has_any_span,
                    "result": result,
                    "facets": {},
                    "back_url_enc": urllib.parse.quote(str(request.url), safe=""),
                    "user_topics_facet": [],
                    "run_id": run_id,
                },
            )

        CHUNK = 200
        id_fqs = []
        for i in range(0, len(doc_ids), CHUNK):
            chunk = doc_ids[i : i + CHUNK]
            inner = " ".join([f'"{_solr_escape_phrase(x)}"' for x in chunk])
            id_fqs.append(f"document_id_s:({inner})")
        fq.append("(" + " OR ".join(id_fqs) + ")")

    params = {
        "q": effective_q,
        "core": core,
        "rows": rows,
        "start": start_i,
        "fq": fq,
        "fl": fl,
        "include_facets": "1",
    }

    if scope == "project":
        params["project_id"] = project_id

    result = await asgi_get(request, "/search", params=params)

    docs = result.get("docs", []) or []
    doc_ids_on_page = [d.get("document_id_s") for d in docs if isinstance(d, dict) and d.get("document_id_s")]

    # ✅ Enrich docs with USER topics from Postgres (no global topics)
    topics_map: dict[str, list[str]] = {}
    user_topics_facet: list[dict] = []

    if run_id and doc_ids_on_page:
        db = SessionLocal()
        try:
            rows2 = db.execute(
                select(DocumentTopic.document_id, DocumentTopic.topic_label)
                .where(DocumentTopic.run_id == UUID(run_id))
                .where(DocumentTopic.status == "active")
                .where(DocumentTopic.document_id.in_(doc_ids_on_page))
            ).all()

            for did, lab in rows2:
                if did and lab:
                    topics_map.setdefault(str(did), []).append(str(lab))

            # facet_rows = db.execute(
            #     select(DocumentTopic.topic_label, func.count())
            #     .where(DocumentTopic.run_id == UUID(run_id))
            #     .where(DocumentTopic.status == "active")
            #     .where(DocumentTopic.document_id.in_(doc_ids_on_page))
            #     .group_by(DocumentTopic.topic_label)
            #     .order_by(func.count().desc(), DocumentTopic.topic_label.asc())
            #     .limit(50)
            # ).all()
            # ---- USER topic facet counts ----
            facet_q = (
                select(DocumentTopic.topic_label, func.count())
                .where(DocumentTopic.run_id == UUID(run_id))
                .where(DocumentTopic.status == "active")
            )

            # Project scope: keep it restricted to the current page (existing behaviour)
            # All corpus: show counts across the entire user's run (global within user scope)
            if scope == "project":
                facet_q = facet_q.where(DocumentTopic.document_id.in_(doc_ids_on_page))

            facet_rows = db.execute(
                facet_q
                .group_by(DocumentTopic.topic_label)
                .order_by(func.count().desc(), DocumentTopic.topic_label.asc())
                .limit(50)
            ).all()

            user_topics_facet = [{"value": lab, "count": int(cnt)} for lab, cnt in facet_rows if lab]
            # user_topics_facet = [{"value": lab, "count": int(cnt)} for lab, cnt in facet_rows if lab]
        finally:
            db.close()

    for d in docs:
        did = d.get("document_id_s") if isinstance(d, dict) else None
        if did:
            d["user_topics_ss"] = topics_map.get(did, [])

    result["docs"] = docs
    facets = result.get("facets", {}) or {}
    # back_url_enc = urllib.parse.quote(str(request.url), safe="")
    back_rel = request.url.path
    if request.url.query:
        back_rel += "?" + request.url.query
    back_url_enc = urllib.parse.quote(back_rel, safe="")

    return request.app.state.templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "user": user,
            "core": core,
            "project_id": project_id,
            "project_name": project_name,
            "scope": scope,
            "q": advanced_q,
            "kw": kw_clean,
            "kw_field": kw_field,
            "include_codes_topics": include_codes_topics or "1",
            "start": start_i,
            "code": code,
            "topic": topic,
            "has_human": has_human,
            "has_any_span": has_any_span,
            "result": result,
            "facets": facets,
            "back_url_enc": back_url_enc,
            "user_topics_facet": user_topics_facet,
            "run_id": run_id,
        },
    )



# ============================================================
# Add to Project
# ============================================================
@router.get("/add_to_project", response_class=HTMLResponse)
async def ui_add_to_project_page(request: Request, user=Depends(require_paid_user)):
    project_id = await _ensure_project_selected(request)
    if not project_id:
        return RedirectResponse("/ui/dashboard?msg=Please%20select%20a%20project%20first", status_code=303)

    return request.app.state.templates.TemplateResponse(
        "add_to_project.html",
        {
            "request": request,
            "user": user,
            "project_id": project_id,
            "project_name": _get_project_name(request),
            "message": None,
        },
    )


@router.post("/add_to_project")
async def ui_add_to_project_post(
    request: Request,
    document_ids_text: str = Form(""),
    user=Depends(require_role("admin", "reviewer")),
):
    project_id = await _ensure_project_selected(request)
    if not project_id:
        return RedirectResponse("/ui/dashboard", status_code=303)

    ids = [x.strip() for x in (document_ids_text or "").splitlines() if x.strip()]
    if not ids:
        return request.app.state.templates.TemplateResponse(
            "add_to_project.html",
            {
                "request": request,
                "user": user,
                "project_id": project_id,
                "project_name": _get_project_name(request),
                "message": "No document IDs provided.",
            },
            status_code=400,
        )

    res = await asgi_post_json(
        request,
        f"/projects/{project_id}/documents/add",
        {"document_ids": ids},
    )

    msg = f"Added: {res.get('docs_added', 0)} | Solr updated: {res.get('solr_docs_updated', 0)}"
    return request.app.state.templates.TemplateResponse(
        "add_to_project.html",
        {
            "request": request,
            "user": user,
            "project_id": project_id,
            "project_name": _get_project_name(request),
            "message": msg,
        },
    )


@router.post("/projects/add_one")
async def ui_add_one_from_search(
    request: Request,
    document_id: str = Form(...),
    next: str | None = Form(None),
    # user=Depends(require_role("admin", "reviewer")),
    user=Depends(require_paid_user)
):
    project_id = await _ensure_project_selected(request)
    if not project_id:
        return RedirectResponse("/ui/dashboard", status_code=303)

    await asgi_post_json(
        request,
        f"/projects/{project_id}/documents/add",
        {"document_ids": [document_id]},
    )

    core = (request.query_params.get("core") or request.session.get("core") or os.getenv("SOLR_GLOBAL_CORE") or "hitl_test").strip()
    if not core:
        core = "hitl_test"
    request.session["core"] = core

    if next:
        try:
            target = urllib.parse.unquote(next)
            if target.startswith("/"):
                return RedirectResponse(target, status_code=303)
            u = urllib.parse.urlparse(target)
            if u.scheme in ("http", "https") and (u.netloc == "" or u.netloc == "app" or u.netloc == "localhost:8000"):
                return RedirectResponse(target, status_code=303)
        except Exception:
            pass

    ref = request.headers.get("referer")
    if ref:
        return RedirectResponse(ref, status_code=303)

    return RedirectResponse(f"/ui/search?core={core}", status_code=303)


# ============================================================
# Export
# ============================================================
@router.get("/export", response_class=HTMLResponse)
async def ui_export_page(
    request: Request,
    version: str = "all",
    code: str | None = None,
    include_annotators: str | None = None,
    metric: str = "value",
    user=Depends(require_paid_user),
):
    project_id = await _ensure_project_selected(request)
    if not project_id:
        return RedirectResponse("/ui/dashboard?msg=Please%20select%20a%20project%20first", status_code=303)

    codes_res = await asgi_get(request, "/codes", params={})
    codes = codes_res.get("codes", []) or []

    return request.app.state.templates.TemplateResponse(
        "export.html",
        {
            "request": request,
            "user": user,
            "project_id": project_id,
            "project_name": _get_project_name(request),
            "codes": codes,
            "version": version,
            "code": code,
            "include_annotators": include_annotators,
            "metric": metric,
        },
    )


# ============================================================
# Codes page
# ============================================================
@router.get("/codes", response_class=HTMLResponse)
async def ui_codes_page(
    request: Request,
    include_inactive: str | None = None,
    user=Depends(require_paid_user),
):
    res = await asgi_get(request, "/codes", params={"include_inactive": bool(include_inactive)})
    return request.app.state.templates.TemplateResponse(
        "codes.html",
        {
            "request": request,
            "user": user,
            "codes": res.get("codes", []) or [],
            "include_inactive": include_inactive,
            "message": None,
        },
    )


@router.post("/codes/create")
async def ui_codes_create(
    request: Request,
    code: str = Form(...),
    display_name: str = Form(""),
    description: str = Form(""),
    user=Depends(require_role("admin")),
):
    await asgi_post_json(
        request,
        "/codes",
        {
            "code": code,
            "display_name": display_name or None,
            "description": description or None,
        },
    )
    return RedirectResponse("/ui/codes", status_code=303)


@router.post("/codes/add_alias")
async def ui_codes_add_alias(
    request: Request,
    code: str = Form(...),
    alias: str = Form(...),
    user=Depends(require_role("admin")),
):
    await asgi_post_json(request, f"/codes/{code}/aliases", {"alias": alias})
    return RedirectResponse("/ui/codes", status_code=303)


@router.post("/codes/deactivate")
async def ui_codes_deactivate(
    request: Request,
    code: str = Form(...),
    user=Depends(require_role("admin")),
):
    await asgi_patch(request, f"/codes/{code}/deactivate")
    return RedirectResponse("/ui/codes", status_code=303)


# ============================================================
# Topic run resolver
# ============================================================
def _get_active_topic_run_id(db, user_id: Optional[str]) -> Optional[str]:
    """
    Resolve the active topic run ID for THIS USER only.
    No project scoping.
    """
    if not user_id:
        return None

    r = (
        db.execute(
            select(TopicRun)
            .where(TopicRun.created_by == str(user_id))
            .where(TopicRun.is_active.is_(True))
            .order_by(TopicRun.created_at.desc())
        )
        .scalars()
        .first()
    )
    return str(r.run_id) if r else None




# ============================================================
# Codes view helpers (from your snippet)
# ============================================================
def _kv_list_to_map(kv_list: Any) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}

    if kv_list is None:
        return out

    if isinstance(kv_list, str):
        items = [kv_list]
    elif isinstance(kv_list, list):
        items = kv_list
    else:
        items = [kv_list]

    def _add(k: str, v: Any) -> None:
        k = (k or "").strip()
        if not k:
            return
        if v is None:
            return
        v_str = str(v).strip()
        if v_str == "":
            return
        out.setdefault(k, []).append(v_str)

    seps = ["=", ":", "|", "\t"]

    for item in items:
        if item is None:
            continue

        if isinstance(item, dict):
            k = item.get("code") or item.get("key") or item.get("k") or item.get("name")
            v = item.get("value") or item.get("val") or item.get("v") or item.get("text")
            if k is not None and v is not None:
                _add(str(k), v)
            continue

        s = str(item).strip()
        if not s:
            continue

        k = None
        v = None
        for sep in seps:
            if sep in s:
                k, v = s.split(sep, 1)
                break
        if k is None:
            continue

        _add(k, v)

    return out


def build_codes_view(doc: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    def _first_present(*keys: str) -> Any:
        for k in keys:
            if k in doc and doc.get(k) is not None:
                return doc.get(k)
        return None

    human_raw = _kv_list_to_map(_first_present("code_value_human_kv_ss", "code_values_human_kv_ss"))
    human_norm = _kv_list_to_map(_first_present("code_value_human_norm_kv_ss", "code_values_human_norm_kv_ss"))
    model_raw = _kv_list_to_map(_first_present("code_value_model_kv_ss", "code_values_model_kv_ss"))
    model_norm = _kv_list_to_map(_first_present("code_value_model_norm_kv_ss", "code_values_model_norm_kv_ss"))

    all_codes: Set[str] = set(human_raw) | set(human_norm) | set(model_raw) | set(model_norm)

    rows: List[Dict[str, Any]] = []
    disagree_count = 0

    def _norm_set(values: List[str]) -> Set[str]:
        return {str(v).strip().lower() for v in (values or []) if str(v).strip()}

    for code in sorted(all_codes):
        hr = human_raw.get(code, [])
        hn = human_norm.get(code, [])
        mr = model_raw.get(code, [])
        mn = model_norm.get(code, [])

        has_human = bool(hr or hn)
        has_model = bool(mr or mn)

        a = _norm_set(hn) if hn else _norm_set(hr)
        b = _norm_set(mn) if mn else _norm_set(mr)
        disagree = bool(has_human and has_model and a and b and a != b)
        if disagree:
            disagree_count += 1

        best = None
        src = None
        if hn:
            best = hn[0]
            src = "human"
        elif hr:
            best = hr[0]
            src = "human"
        elif mn:
            best = mn[0]
            src = "model"
        elif mr:
            best = mr[0]
            src = "model"

        if has_human and has_model:
            src = "both"

        rows.append(
            {
                "code": code,
                "has_human": has_human,
                "has_model": has_model,
                "human_raw": hr,
                "human_norm": hn,
                "model_raw": mr,
                "model_norm": mn,
                "best": best,
                "source": src,
                "disagree": disagree,
            }
        )

    stats = {
        "total": len(all_codes),
        "human_present": sum(1 for r in rows if r["has_human"]),
        "model_present": sum(1 for r in rows if r["has_model"]),
        "human_only": sum(1 for r in rows if r["has_human"] and not r["has_model"]),
        "model_only": sum(1 for r in rows if r["has_model"] and not r["has_human"]),
        "both": sum(1 for r in rows if r["has_human"] and r["has_model"]),
        "disagree": disagree_count,
    }
    return rows, stats


# ============================================================
# Document detail
# ============================================================
@router.get("/docs/{document_id}", response_class=HTMLResponse)
async def ui_doc_detail(request: Request, document_id: str, user=Depends(require_paid_user)):
    import os
    import urllib.parse

    core = (request.query_params.get("core") or request.session.get("core") or os.getenv("SOLR_GLOBAL_CORE") or "hitl_test").strip()
    if not core:
        core = "hitl_test"
    request.session["core"] = core

    # ✅ DO NOT force project selection. Use it if present; otherwise allow All-corpus browsing.
    project_id = _get_project_id(request)
    project_name = _get_project_name(request)

    # Check doc is in current project (only if a project is selected)
    in_project = False
    if project_id:
        try:
            proj_res = await asgi_get(
                request,
                f"/projects/{project_id}/documents",
                params={"limit": 5000, "offset": 0},
            )
            ids = set(proj_res.get("document_ids", []) or [])
            in_project = document_id in ids
        except Exception:
            in_project = False

    # Fetch document from Solr
    doc_res = await asgi_get(
        request,
        "/search",
        params={
            "q": f'document_id_s:"{_solr_escape_phrase(document_id)}"',
            "core": core,
            "rows": 1,
            "start": 0,
            "include_facets": "0",
            "fl": ",".join(
                [
                    "document_id_s",
                    "canonical_url_s",
                    "title_txt",
                    "excerpt_txt",
                    "published_dt",
                    "doc_type_s",
                    "source_s",
                    "has_human_b",
                    "has_model_b",
                    "codes_present_human_ss",
                    "codes_present_model_ss",
                    "code_value_human_kv_ss",
                    "code_value_human_norm_kv_ss",
                    "code_value_model_kv_ss",
                    "code_value_model_norm_kv_ss",
                ]
            ),
        },
    )
    docs = doc_res.get("docs", []) or []
    doc = docs[0] if docs else {"document_id_s": document_id, "title_txt": ["(not found in Solr)"]}

    # Resolve run_id per USER (NO global)
    uid = user.get("id") or user.get("user_id") or user.get("sub")
    run_id = _get_run_id(request)

    if not run_id:
        db = SessionLocal()
        try:
            run_id = _get_active_topic_run_id(db, user_id=str(uid) if uid else None)
        finally:
            db.close()
        _set_run_id(request, run_id)

    # Load topics for this run (if exists)
    topics: list[dict] = []
    if run_id:
        topics_res = await asgi_get(
            request,
            f"/documents/{document_id}/topics",
            params={"run_id": run_id},
        )
        topics = topics_res.get("topics", []) or []

    codes_view, code_stats = build_codes_view(doc)

    def _normalize_back_url(u: str | None) -> str:
        """
        Accepts encoded relative or absolute URLs.
        Returns a safe relative path + query.
        """
        if not u:
            return ""
        u = urllib.parse.unquote(u)
        p = urllib.parse.urlparse(u)
        if p.scheme and p.netloc:
            out = p.path or "/ui/search"
            if p.query:
                out += "?" + p.query
            return out
        if u.startswith("/"):
            return u
        return "/ui/search"

    back_url_raw = request.query_params.get("back_url") or request.headers.get("referer")
    back_url = _normalize_back_url(back_url_raw) or f"/ui/search?core={core}"

    # keep return_to stable
    return_to = request.url.path + f"?core={core}"

    return request.app.state.templates.TemplateResponse(
        "doc_detail.html",
        {
            "request": request,
            "user": user,
            "project_id": project_id,      # may be None
            "project_name": project_name,
            "core": core,
            "doc": doc,
            "run_id": run_id,
            "topics": topics,
            "in_project": in_project,
            "back_url": back_url,
            "return_to": return_to,
            "codes_view": codes_view,
            "code_stats": code_stats,
        },
    )

# ============================================================
# Topic actions
@router.post("/topics/accept")
async def ui_topic_accept(
    request: Request,
    document_id: str = Form(...),
    topic_label: str = Form(...),
    run_id: str = Form(""),
    topic_key: str = Form(""),
    user=Depends(require_paid_user),
):
    form = await request.form()

    doc_id = (form.get("document_id") or "").strip()
    topic_label = (form.get("topic_label") or "").strip()
    topic_key = (form.get("topic_key") or "").strip()
    run_id = (form.get("run_id") or "").strip()

    if not doc_id or not topic_label:
        raise HTTPException(400, "document_id and topic_label are required")

    # Keep this if you want topic actions only inside a selected project (permission gate).
    project_id = await _ensure_project_selected(request)
    if not project_id:
        return RedirectResponse(
            "/ui/dashboard?msg=Please%20select%20a%20project%20first",
            status_code=303,
        )

    uid = user.get("id") or user.get("user_id") or user.get("sub")
    if not uid:
        raise HTTPException(401, "Not authenticated")

    # USER-ONLY run resolution
    if not run_id:
        db = SessionLocal()
        try:
            # IMPORTANT: this helper must be USER-only.
            # It should return the active run_id for created_by == uid (project_id ignored / NULL).
            run_id = _get_active_topic_run_id(db, user_id=str(uid))
        finally:
            db.close()

    # Store run in session for subsequent actions (delete/reject/etc.)
    if run_id:
        _set_run_id(request, run_id)

    payload = {
        "project_id": project_id,      # still used by API assert_project_member(...) as a permission gate
        "run_id": run_id or None,      # API will create user-only run if None
        "document_id": doc_id,
        "topic_label": topic_label,
    }

    # Only include topic_key if user/UI provided it
    if topic_key:
        payload["topic_key"] = topic_key

    await asgi_post_json(request, "/topics/label", payload)
    return RedirectResponse(f"/ui/docs/{doc_id}", status_code=303)





@router.post("/topics/reject")
async def ui_topic_reject(
    request: Request,
    document_id: str = Form(...),
    topic_key: str = Form(...),
    run_id: str = Form(...),
    user=Depends(require_paid_user),
):
    await asgi_post_json(
        request,
        "/topics/reject",
        {
            "run_id": (run_id or "").strip(),
            "document_id": (document_id or "").strip(),
            "topic_key": (topic_key or "").strip(),
        },
    )
    return RedirectResponse(f"/ui/docs/{document_id}", status_code=303)




@router.post("/topics/delete")
async def ui_topic_delete(
    request: Request,
    document_id: str = Form(...),
    topic_key: str = Form(...),
    run_id: str = Form(...),
    user=Depends(require_paid_user),
):
    await asgi_post_json(
        request,
        "/topics/label/delete",
        {
            "run_id": (run_id or "").strip(),
            "document_id": (document_id or "").strip(),
            "topic_key": (topic_key or "").strip(),
        },
    )
    return RedirectResponse(f"/ui/docs/{document_id}", status_code=303)




# ============================================================
# Projects (create/bootstrap)
# ============================================================
@router.post("/projects/create")
async def ui_create_project(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    team_name: str = Form("Default Team"),
    # user=Depends(require_role("admin", "reviewer")),
    user=Depends(require_paid_user),
):
    name_clean = (name or "").strip()
    desc = (description or "").strip()

    if not name_clean:
        return RedirectResponse("/ui/dashboard?msg=Project%20name%20is%20required", status_code=303)

    if len(desc) > 1000:
        return RedirectResponse("/ui/dashboard?msg=Description%20must%20be%201000%20characters%20or%20less", status_code=303)

    uid = user.get("id") or user.get("user_id") or user.get("sub")
    if uid is None:
        return RedirectResponse("/ui/dashboard?msg=Could%20not%20determine%20your%20user%20id", status_code=303)

    created = await asgi_post_json(
        request,
        "/projects/bootstrap",
        {
            "name": name_clean,
            "team_name": team_name or "Default Team",
            "description": desc,
            "creator_user_id": str(uid),
        },
    )

    project_id = created.get("project_id")
    if project_id:
        _set_project_id(request, project_id)
        _set_project_name(request, name_clean)
        _set_run_id(request, None)

    return RedirectResponse("/ui/search", status_code=303)

#
#
#
