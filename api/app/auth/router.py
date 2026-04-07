# app/auth/router.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.auth.models import User, AccessCode
from app.auth.security import (
    hash_password, verify_password,
    generate_access_code, hash_access_code, verify_access_code
)
from app.auth.deps import require_user, require_role


import os
import smtplib
from email.message import EmailMessage
import secrets
# import stripe
from datetime import timezone

from app.auth.models import User, AccessCode, PasswordResetToken
from app.auth.security import (
    hash_password, verify_password,
    generate_access_code, hash_access_code, verify_access_code,
    hash_reset_token, verify_reset_token,
)


router = APIRouter(prefix="/auth", tags=["auth"])
# stripe.api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()



def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _set_session_user(request: Request, u: User):
    # Matches what your templates show in navbar (username + role). :contentReference[oaicite:2]{index=2}
    request.session["user"] = {
        "id": str(u.id),
        "email": u.email,
        "username": u.username,
        "role": u.role,
        "is_active": bool(u.is_active),
        "plan": u.plan,
        "access_until": (u.access_until.astimezone(timezone.utc).isoformat() if u.access_until else None),
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return request.app.state.templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    # login.html posts here already :contentReference[oaicite:3]{index=3}
    ident = (username or "").strip().lower()
    if not ident or not password:
        return request.app.state.templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Missing username or password."},
            status_code=400,
        )

    db = SessionLocal()
    try:
        u = db.execute(
            select(User).where((User.username == ident) | (User.email == ident))
        ).scalar_one_or_none()

        if not u or not u.is_active or not verify_password(password, u.password_hash):
            return request.app.state.templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Invalid credentials."},
                status_code=401,
            )

        _set_session_user(request, u)
        return RedirectResponse("/ui/about", status_code=303)
    finally:
        db.close()


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    # Use a template instead of inline HTML
    return request.app.state.templates.TemplateResponse(
        "register.html",
        {"request": request, "error": None},
    )


@router.post("/register", response_class=HTMLResponse)
def register_post(
    request: Request,
    email: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    email = (email or "").strip().lower()
    username = (username or "").strip().lower()

    if not email or "@" not in email:
        return request.app.state.templates.TemplateResponse(
            "register.html", {"request": request, "error": "Please enter a valid email address."}, status_code=400
        )

    if not username or len(username) < 3:
        return request.app.state.templates.TemplateResponse(
            "register.html", {"request": request, "error": "Username must be at least 3 characters."}, status_code=400
        )

    if password != password2:
        return request.app.state.templates.TemplateResponse(
            "register.html", {"request": request, "error": "Passwords do not match."}, status_code=400
        )

    if len(password) < 8:
        return request.app.state.templates.TemplateResponse(
            "register.html", {"request": request, "error": "Password must be at least 8 characters."}, status_code=400
        )

    # bcrypt max is 72 bytes
    if len(password.encode("utf-8")) > 72:
        return request.app.state.templates.TemplateResponse(
            "register.html", {"request": request, "error": "Password too long (max 72 bytes)."}, status_code=400
        )

    db = SessionLocal()
    try:
        now = _now_utc()
        trial_days = 7

        u = User(
            email=email,
            username=username,
            password_hash=hash_password(password),
            role="viewer",
            plan="trial",  # ✅ NOT free, so require_paid_user allows
            access_until=now + timedelta(days=trial_days),
            is_active=True,
        )
        db.add(u)
        db.commit()
        db.refresh(u)

        _set_session_user(request, u)
        return RedirectResponse("/ui/about", status_code=303)

    except IntegrityError:
        db.rollback()
        return request.app.state.templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Email or username already exists."},
            status_code=409,
        )
    finally:
        db.close()



@router.get("/redeem", response_class=HTMLResponse)
def redeem_page(request: Request, user=Depends(require_user)):
    html = """
    <!doctype html>
    <html>
      <head>
        <title>Redeem Access Code</title>
        <link rel="stylesheet" href="/static/style.css">
      </head>
      <body class="centered">
        <div class="card">
          <h1>Redeem access code</h1>
          <p class="muted">For user testing only.</p>
          <form method="post" action="/auth/redeem">
            <label>Access code</label>
            <input name="code" autocomplete="off" required>
            <button type="submit">Redeem</button>
          </form>
          <p class="muted"><a href="/ui/dashboard">Back</a></p>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


@router.post("/redeem")
def redeem_post(request: Request, code: str = Form(...), user=Depends(require_user)):
    code = (code or "").strip()
    if not code or len(code) < 10:
        raise HTTPException(status_code=400, detail="Invalid code")

    db = SessionLocal()
    try:
        all_codes = db.execute(select(AccessCode)).scalars().all()

        matched: AccessCode | None = None
        for r in all_codes:
            if verify_access_code(code, r.code_hash):
                matched = r
                break

        if not matched:
            raise HTTPException(status_code=400, detail="Code not found")

        now = _now_utc()
        if matched.expires_at <= now:
            raise HTTPException(status_code=400, detail="Code expired")
        if matched.uses >= matched.max_uses:
            raise HTTPException(status_code=400, detail="Code already used")

        # Optional: lock a code to a specific email
        if matched.allowed_email and matched.allowed_email.lower() != (user.get("email") or "").lower():
            raise HTTPException(status_code=403, detail="Code not valid for this user")

        u = db.execute(select(User).where(User.id == user["id"])).scalar_one()

        # Days granted (default 7 if null)
        days = int(matched.days_granted or 7)

        # Extend from the later of (now) or (current access_until), so repeated redeems extend
        base = now
        if u.access_until:
            try:
                if u.access_until > base:
                    base = u.access_until
            except Exception:
                pass
        until = base + timedelta(days=days)

        # Plan granted: allow "comped", "trial", "paid" etc.
        new_plan = (matched.plan_granted or "trial").lower()
        u.plan = new_plan
        u.access_until = until
        u.updated_at = datetime.utcnow()

        matched.uses += 1
        matched.redeemed_by_user_id = u.id
        matched.redeemed_at = now

        db.commit()
        db.refresh(u)
        _set_session_user(request, u)

        return RedirectResponse("/ui/dashboard", status_code=303)
    finally:
        db.close()


@router.post("/admin/create_code")
def admin_create_code(
    request: Request,
    allowed_email: str = Form(""),
    days_granted: int = Form(7),
    expires_hours: int = Form(48),
    plan_granted: str = Form("trial"),   # can be trial/comped/paid
    max_uses: int = Form(1),             # ✅ allow multi-use codes if you want
    user=Depends(require_role("admin")),
):
    """
    Admin-only endpoint to generate access codes.
    Plaintext returned ONCE; only hash stored.
    """
    allowed_email = (allowed_email or "").strip().lower() or None
    days_granted = max(1, min(int(days_granted), 365))        # cap at 1 year
    expires_hours = max(1, min(int(expires_hours), 24 * 365)) # cap at 1 year
    max_uses = max(1, min(int(max_uses), 1000))               # cap

    plan_granted = (plan_granted or "trial").strip().lower()
    if plan_granted not in {"trial", "comped", "paid"}:
        raise HTTPException(status_code=400, detail="plan_granted must be one of: trial, comped, paid")

    code_plain = generate_access_code(prefix="ACCESS")
    code_h = hash_access_code(code_plain)

    db = SessionLocal()
    try:
        ac = AccessCode(
            code_hash=code_h,
            purpose="access",
            plan_granted=plan_granted,
            days_granted=days_granted,
            allowed_email=allowed_email,
            max_uses=max_uses,
            uses=0,
            expires_at=_now_utc() + timedelta(hours=expires_hours),
            issued_by_user_id=user.get("id"),
        )
        db.add(ac)
        db.commit()

        return JSONResponse(
            {
                "ok": True,
                "code": code_plain,
                "allowed_email": allowed_email,
                "days_granted": days_granted,
                "expires_at": ac.expires_at.isoformat(),
                "plan_granted": plan_granted,
                "max_uses": max_uses,
            }
        )
    finally:
        db.close()


@router.get("/admin/codes", response_class=HTMLResponse)
def admin_codes_page(request: Request, user=Depends(require_role("admin"))):
    db = SessionLocal()
    try:
        rows = db.execute(
            select(AccessCode).order_by(AccessCode.expires_at.desc())
        ).scalars().all()

        # Build a safe view (NO plaintext)
        now = _now_utc()
        codes = []
        for r in rows[:50]:
            expired = bool(r.expires_at and r.expires_at <= now)
            used_up = bool(r.max_uses is not None and r.uses is not None and r.uses >= r.max_uses)
            status = "expired" if expired else ("used" if used_up else "active")

            codes.append(
                {
                    "purpose": r.purpose,
                    "plan_granted": r.plan_granted,
                    "days_granted": r.days_granted,
                    "allowed_email": r.allowed_email,
                    "uses": r.uses,
                    "max_uses": r.max_uses,
                    "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                    "status": status,
                    "redeemed_at": r.redeemed_at.isoformat() if getattr(r, "redeemed_at", None) else None,
                    "redeemed_by_user_id": str(r.redeemed_by_user_id) if getattr(r, "redeemed_by_user_id", None) else None,
                    # show only a short fingerprint of the hash for admin debugging
                    "code_hash_prefix": (r.code_hash[:10] + "…") if getattr(r, "code_hash", None) else None,
                }
            )

        return request.app.state.templates.TemplateResponse(
            "admin_codes.html",
            {
                "request": request,
                "user": user,
                "error": None,
                "created": None,
                "codes": codes,
                "message": None,
            },
        )
    finally:
        db.close()


@router.post("/admin/codes", response_class=HTMLResponse)
def admin_codes_create_page(
    request: Request,
    allowed_email: str = Form(""),
    days_granted: int = Form(7),
    expires_hours: int = Form(48),
    plan_granted: str = Form("trial"),
    max_uses: int = Form(1),
    user=Depends(require_role("admin")),
):
    allowed_email = (allowed_email or "").strip().lower() or None

    try:
        days_granted = max(1, min(int(days_granted), 365))
        expires_hours = max(1, min(int(expires_hours), 24 * 365))
        max_uses = max(1, min(int(max_uses), 1000))
    except Exception:
        days_granted, expires_hours, max_uses = 7, 48, 1

    plan_granted = (plan_granted or "trial").strip().lower()
    if plan_granted not in {"trial", "comped", "paid"}:
        plan_granted = "trial"

    code_plain = generate_access_code(prefix="ACCESS")
    code_h = hash_access_code(code_plain)

    db = SessionLocal()
    try:
        ac = AccessCode(
            code_hash=code_h,
            purpose="access",
            plan_granted=plan_granted,
            days_granted=days_granted,
            allowed_email=allowed_email,
            max_uses=max_uses,
            uses=0,
            expires_at=_now_utc() + timedelta(hours=expires_hours),
            issued_by_user_id=user.get("id"),
        )
        db.add(ac)
        db.commit()

        # reload list for display
        rows = db.execute(select(AccessCode).order_by(AccessCode.expires_at.desc())).scalars().all()
        now = _now_utc()
        codes = []
        for r in rows[:50]:
            expired = bool(r.expires_at and r.expires_at <= now)
            used_up = bool(r.max_uses is not None and r.uses is not None and r.uses >= r.max_uses)
            status = "expired" if expired else ("used" if used_up else "active")
            codes.append(
                {
                    "purpose": r.purpose,
                    "plan_granted": r.plan_granted,
                    "days_granted": r.days_granted,
                    "allowed_email": r.allowed_email,
                    "uses": r.uses,
                    "max_uses": r.max_uses,
                    "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                    "status": status,
                    "redeemed_at": r.redeemed_at.isoformat() if getattr(r, "redeemed_at", None) else None,
                    "redeemed_by_user_id": str(r.redeemed_by_user_id) if getattr(r, "redeemed_by_user_id", None) else None,
                    "code_hash_prefix": (r.code_hash[:10] + "…") if getattr(r, "code_hash", None) else None,
                }
            )

        created = {
            "code": code_plain,  # show ONCE
            "allowed_email": allowed_email,
            "days_granted": days_granted,
            "expires_at": ac.expires_at.isoformat(),
            "plan_granted": plan_granted,
            "max_uses": max_uses,
        }

        return request.app.state.templates.TemplateResponse(
            "admin_codes.html",
            {"request": request,
             "user": user,
             "error": None,
             "created": created,
             "codes": codes,
             "message": None,
             },
        )
    finally:
        db.close()

@router.post("/admin/codes/send_email", response_class=HTMLResponse)
def admin_send_code_email(
    request: Request,
    recipient_email: str = Form(...),     # who receives the email
    days_granted: int = Form(7),
    expires_hours: int = Form(48),
    plan_granted: str = Form("trial"),
    max_uses: int = Form(1),
    user=Depends(require_role("admin")),
):
    """
    Admin-only: generate an access code and EMAIL it to the recipient.
    Plaintext code is never stored in DB.
    """
    recipient_email = (recipient_email or "").strip().lower()
    if not recipient_email or "@" not in recipient_email:
        return request.app.state.templates.TemplateResponse(
            "admin_codes.html",
            {"request": request, "user": user, "error": "Please enter a valid recipient email.", "created": None, "message": None, "codes": []},
            status_code=400,
        )

    # clamp inputs (same safety as your create endpoint)
    try:
        days_granted = max(1, min(int(days_granted), 365))
        expires_hours = max(1, min(int(expires_hours), 24 * 365))
        max_uses = max(1, min(int(max_uses), 1000))
    except Exception:
        days_granted, expires_hours, max_uses = 7, 48, 1

    plan_granted = (plan_granted or "trial").strip().lower()
    if plan_granted not in {"trial", "comped", "paid"}:
        plan_granted = "trial"

    code_plain = generate_access_code(prefix="ACCESS")
    code_h = hash_access_code(code_plain)

    db = SessionLocal()
    try:
        ac = AccessCode(
            code_hash=code_h,
            purpose="access",
            plan_granted=plan_granted,
            days_granted=days_granted,
            allowed_email=recipient_email,  # lock to the recipient (prevents sharing)
            max_uses=max_uses,
            uses=0,
            expires_at=_now_utc() + timedelta(hours=expires_hours),
            issued_by_user_id=user.get("id"),
        )
        db.add(ac)
        db.commit()

        # Send email
        base = _public_base_url(request)
        redeem_link = f"{base}/auth/redeem"


        expires_readable = ac.expires_at.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

        subject = "Your JuDDGES Access Code"

        body = (
            f"Hello,\n\n"
            f"You have been granted access to JuDDGES.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔐 ACCESS CODE\n"
            f"{code_plain}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Details:\n"
            f"• Plan: {plan_granted.capitalize()}\n"
            f"• Access duration: {days_granted} day(s)\n"
            f"• Code valid until: {expires_readable}\n\n"
            f"To activate your access:\n"
            f"1) Log in to your account\n"
            f"2) Visit: {redeem_link}\n"
            f"3) Enter the access code above\n\n"
            f"If you did not expect this email, you can safely ignore it.\n\n"
            f"— JuDDGES Team\n"
        )

        _send_email(to_email=recipient_email, subject=subject, body=body)

        # Reload list for display
        rows = db.execute(select(AccessCode).order_by(AccessCode.expires_at.desc())).scalars().all()
        now = _now_utc()
        codes = []
        for r in rows[:50]:
            expired = bool(r.expires_at and r.expires_at <= now)
            used_up = bool(r.max_uses is not None and r.uses is not None and r.uses >= r.max_uses)
            status = "expired" if expired else ("used" if used_up else "active")
            codes.append(
                {
                    "purpose": r.purpose,
                    "plan_granted": r.plan_granted,
                    "days_granted": r.days_granted,
                    "allowed_email": r.allowed_email,
                    "uses": r.uses,
                    "max_uses": r.max_uses,
                    "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                    "status": status,
                    "redeemed_at": r.redeemed_at.isoformat() if getattr(r, "redeemed_at", None) else None,
                    "redeemed_by_user_id": str(r.redeemed_by_user_id) if getattr(r, "redeemed_by_user_id", None) else None,
                    "code_hash_prefix": (r.code_hash[:10] + "…") if getattr(r, "code_hash", None) else None,
                }
            )

        msg = f"Access code emailed to {recipient_email} (locked to that email)."

        return request.app.state.templates.TemplateResponse(
            "admin_codes.html",
            {"request": request, "user": user, "error": None, "created": None, "message": msg, "codes": codes},
        )
    finally:
        db.close()

@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, user=Depends(require_role("admin"))):
    db = SessionLocal()
    try:
        users = db.execute(select(User).order_by(User.created_at.desc())).scalars().all()

        rows = []
        for u in users[:200]:
            rows.append(
                {
                    "id": str(u.id),
                    "email": u.email,
                    "username": u.username,
                    "role": u.role,
                    "plan": u.plan,
                    "is_active": bool(u.is_active),
                    "access_until": u.access_until.isoformat() if u.access_until else None,
                    "created_at": u.created_at.isoformat() if getattr(u, "created_at", None) else None,
                }
            )

        return request.app.state.templates.TemplateResponse(
            "admin_users.html",
            {"request": request, "user": user, "rows": rows, "error": None, "message": None},
        )
    finally:
        db.close()


@router.post("/admin/users/revoke", response_class=HTMLResponse)
def admin_revoke_user_access(
    request: Request,
    user_id: str = Form(...),
    user=Depends(require_role("admin")),
):
    db = SessionLocal()
    try:
        u = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
        if not u:
            return request.app.state.templates.TemplateResponse(
                "admin_users.html",
                {"request": request, "user": user, "rows": [], "error": "User not found.", "message": None},
                status_code=404,
            )

        # Minimal "revoke paid access"
        u.plan = "free"
        u.access_until = None
        u.updated_at = datetime.utcnow()

        db.commit()
        return RedirectResponse("/auth/admin/users", status_code=303)
    finally:
        db.close()



# def _send_email(to_email: str, subject: str, body: str) -> None:
#     """
#     Minimal SMTP sender.
#     If SMTP_* not set, we just log the email to console (dev mode).
#     """
#     host = os.getenv("SMTP_HOST", "").strip()
#     port = int(os.getenv("SMTP_PORT", "587"))
#     user = os.getenv("SMTP_USER", "").strip()
#     pwd = os.getenv("SMTP_PASS", "").strip()
#     sender = os.getenv("SMTP_FROM", user).strip()
#
#     if not host or not sender:
#         print("\n=== DEV EMAIL (SMTP not configured) ===")
#         print("TO:", to_email)
#         print("SUBJECT:", subject)
#         print(body)
#         print("=== END ===\n")
#         return
#
#     msg = EmailMessage()
#     msg["From"] = sender
#     msg["To"] = to_email
#     msg["Subject"] = subject
#     msg.set_content(body)
#
#     with smtplib.SMTP(host, port, timeout=10) as s:
#         s.ehlo()
#         s.starttls()
#         s.ehlo()
#         if user and pwd:
#             s.login(user, pwd)
#         s.send_message(msg)
import ssl
import smtplib
from email.message import EmailMessage

def _send_email(to_email: str, subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    pwd = os.getenv("SMTP_PASS", "").strip()
    sender = os.getenv("SMTP_FROM", user).strip()

    if not host or not sender:
        print("\n=== DEV EMAIL (SMTP not configured) ===")
        print("TO:", to_email)
        print("SUBJECT:", subject)
        print(body)
        print("=== END ===\n")
        return

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    timeout = int(os.getenv("SMTP_TIMEOUT", "15"))

    # TLS context (safe defaults)
    ctx = ssl.create_default_context()

    # IMPORTANT: use explicit timeouts and EHLO before/after STARTTLS
    with smtplib.SMTP(host, port, timeout=timeout) as s:
        s.set_debuglevel(1)
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()

        if user and pwd:
            s.login(user, pwd)

        s.send_message(msg)


def _public_base_url(request: Request) -> str:
    # Prefer env if behind nginx; else derive from request
    base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base:
        return base
    # fallback
    scheme = request.url.scheme
    host = request.headers.get("host") or "localhost:8000"
    return f"{scheme}://{host}"


@router.get("/forgot", response_class=HTMLResponse)
def forgot_page(request: Request):
    return request.app.state.templates.TemplateResponse("forgot_password.html", {"request": request, "error": None, "ok": None})


@router.post("/forgot", response_class=HTMLResponse)
def forgot_post(request: Request, email: str = Form(...)):
    email = (email or "").strip().lower()

    # Always show a generic success (avoid account enumeration)
    generic_ok = "If that email exists, we sent a reset link."

    db = SessionLocal()
    try:
        u = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if not u or not u.is_active:
            return request.app.state.templates.TemplateResponse(
                "forgot_password.html", {"request": request, "error": None, "ok": generic_ok}
            )

        token_plain = secrets.token_urlsafe(48)
        token_hash = hash_reset_token(token_plain)
        now = _now_utc()
        expires = now + timedelta(minutes=int(os.getenv("RESET_TOKEN_MINUTES", "30")))

        prt = PasswordResetToken(
            user_id=u.id,
            token_hash=token_hash,
            expires_at=expires,
        )
        db.add(prt)
        db.commit()

        base = _public_base_url(request)
        link = f"{base}/auth/reset?token={token_plain}"
        _send_email(
            to_email=u.email,
            subject="Reset your password",
            body=f"Use this link to reset your password (valid for 30 minutes):\n\n{link}\n",
        )

        return request.app.state.templates.TemplateResponse(
            "forgot_password.html", {"request": request, "error": None, "ok": generic_ok}
        )
    finally:
        db.close()


@router.get("/reset", response_class=HTMLResponse)
def reset_page(request: Request, token: str = ""):
    token = (token or "").strip()
    return request.app.state.templates.TemplateResponse(
        "reset_password.html", {"request": request, "token": token, "error": None}
    )


@router.post("/reset", response_class=HTMLResponse)
def reset_post(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    token = (token or "").strip()

    if not token or len(token) < 20:
        return request.app.state.templates.TemplateResponse(
            "reset_password.html", {"request": request, "token": token, "error": "Invalid or missing token."}, status_code=400
        )

    if password != password2:
        return request.app.state.templates.TemplateResponse(
            "reset_password.html", {"request": request, "token": token, "error": "Passwords do not match."}, status_code=400
        )

    if len(password) < 8:
        return request.app.state.templates.TemplateResponse(
            "reset_password.html", {"request": request, "token": token, "error": "Password must be at least 8 characters."}, status_code=400
        )

    if len(password.encode("utf-8")) > 72:
        return request.app.state.templates.TemplateResponse(
            "reset_password.html", {"request": request, "token": token, "error": "Password too long (max 72 bytes)."}, status_code=400
        )

    db = SessionLocal()
    try:
        now = _now_utc()
        # Find matching token row by verifying hash (same pattern you used for AccessCode scanning):contentReference[oaicite:4]{index=4}
        rows = db.execute(select(PasswordResetToken).where(PasswordResetToken.used_at.is_(None))).scalars().all()

        matched = None
        for r in rows:
            if r.expires_at and r.expires_at <= now:
                continue
            if verify_reset_token(token, r.token_hash):
                matched = r
                break

        if not matched:
            return request.app.state.templates.TemplateResponse(
                "reset_password.html",
                {"request": request, "token": token, "error": "Reset link is invalid or expired."},
                status_code=400,
            )

        u = db.get(User, matched.user_id)
        if not u or not u.is_active:
            return request.app.state.templates.TemplateResponse(
                "reset_password.html",
                {"request": request, "token": token, "error": "Account not available."},
                status_code=400,
            )

        u.password_hash = hash_password(password)  # already used in register:contentReference[oaicite:5]{index=5}
        u.updated_at = datetime.utcnow()

        matched.used_at = now
        matched.used_ip = (request.client.host if request.client else None)

        db.commit()

        # Optional: log them in immediately
        _set_session_user(request, u)
        return RedirectResponse("/ui/dashboard?msg=Password%20updated", status_code=303)
    finally:
        db.close()

@router.get("/request_access", response_class=HTMLResponse)
def request_access_page(request: Request, user=Depends(require_user)):
    return request.app.state.templates.TemplateResponse(
        "request_access.html",
        {"request": request, "user": user, "error": None, "ok": None},
    )


@router.post("/request_access", response_class=HTMLResponse)
def request_access_post(
    request: Request,
    message: str = Form(""),
    user=Depends(require_user),
):
    """
    Logged-in user requests an access code / extension.
    Emails the admin address using SMTP. No DB changes.
    """
    print(">>> request_access_post HIT", user)
    admin_email = (os.getenv("ADMIN_EMAIL") or "").strip()
    if not admin_email:
        return request.app.state.templates.TemplateResponse(
            "request_access.html",
            {
                "request": request,
                "user": user,
                "error": "ADMIN_EMAIL is not configured on the server.",
                "ok": None,
            },
            status_code=500,
        )

    msg = (message or "").strip()
    if len(msg) > 2000:
        msg = msg[:2000]

    base = _public_base_url(request)

    # user dict (from session)
    u_email = (user.get("email") or "").strip()
    u_username = (user.get("username") or "").strip()
    u_plan = (user.get("plan") or "").strip()
    u_access_until = user.get("access_until") or "N/A"
    u_id = user.get("id") or "N/A"

    subject = f"JuDDGES access request: {u_username or u_email}"

    body = (
        f"A user requested access / trial extension.\n\n"
        f"User details:\n"
        f"• Username: {u_username}\n"
        f"• Email: {u_email}\n"
        f"• User ID: {u_id}\n"
        f"• Current plan: {u_plan}\n"
        f"• Access until: {u_access_until}\n\n"
        f"Suggested next step:\n"
        f"1) Go to {base}/auth/admin/codes\n"
        f"2) Use “Send Access Code by Email” to send a code to: {u_email}\n\n"
        f"User message:\n"
        f"{msg if msg else '(no message provided)'}\n"
    )

    print(">>> about to send email to admin:", admin_email)
    try:
        _send_email(to_email=admin_email, subject=subject, body=body)
    except Exception as e:
        print(">>> request_access_post email failed:", repr(e))
        return request.app.state.templates.TemplateResponse(
            "request_access.html",
            {
                "request": request,
                "user": user,
                "error": "We couldn’t send your request email right now. Please try again in a minute.",
                "ok": None,
            },
            status_code=200,  # don't show scary 500
        )
    print(">>> email send finished")
    return request.app.state.templates.TemplateResponse(
        "request_access.html",
        {"request": request, "user": user, "error": None, "ok": "Request sent. We’ll email you an access code soon."},
    )


#######################STRIPE###########################

# @router.get("/billing", response_class=HTMLResponse)
# def billing_page(request: Request, user=Depends(require_user)):
#     # Simple page with two buttons: monthly/yearly
#     return request.app.state.templates.TemplateResponse("billing.html", {"request": request, "user": user, "error": None})
#
#
# @router.post("/billing/checkout")
# def billing_checkout(
#     request: Request,
#     price_key: str = Form(...),  # "monthly" or "yearly"
#     user=Depends(require_user),
# ):
#     if not stripe.api_key:
#         raise HTTPException(500, "Stripe is not configured (STRIPE_SECRET_KEY missing)")
#
#     uid = user.get("id")
#     if not uid:
#         raise HTTPException(401, "Not authenticated")
#
#     price_id = None
#     if price_key == "monthly":
#         price_id = (os.getenv("STRIPE_PRICE_PRO_MONTHLY") or "").strip()
#     elif price_key == "yearly":
#         price_id = (os.getenv("STRIPE_PRICE_PRO_YEARLY") or "").strip()
#
#     if not price_id:
#         raise HTTPException(500, "Stripe price not configured")
#
#     base = _public_base_url(request)
#     success_url = f"{base}/ui/dashboard?msg=Payment%20successful"
#     cancel_url = f"{base}/auth/billing?error=cancelled"
#
#     # Create/reuse customer
#     db = SessionLocal()
#     try:
#         u = db.get(User, uid)
#         if not u:
#             raise HTTPException(404, "User not found")
#
#         customer_id = (u.stripe_customer_id or "").strip() or None
#         if not customer_id:
#             cust = stripe.Customer.create(email=u.email, metadata={"user_id": str(u.id)})
#             customer_id = cust["id"]
#             u.stripe_customer_id = customer_id
#             u.updated_at = datetime.utcnow()
#             db.commit()
#
#         session = stripe.checkout.Session.create(
#             mode="subscription",
#             customer=customer_id,
#             line_items=[{"price": price_id, "quantity": 1}],
#             success_url=success_url,
#             cancel_url=cancel_url,
#             client_reference_id=str(u.id),
#             metadata={"user_id": str(u.id), "price_key": price_key},
#         )
#         return RedirectResponse(session.url, status_code=303)
#     finally:
#         db.close()
#
#
# @router.post("/stripe/webhook")
# async def stripe_webhook(request: Request):
#     secret = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
#     if not secret:
#         raise HTTPException(500, "STRIPE_WEBHOOK_SECRET missing")
#
#     payload = await request.body()
#     sig = request.headers.get("stripe-signature", "")
#
#     try:
#         event = stripe.Webhook.construct_event(payload=payload, sig_header=sig, secret=secret)
#     except Exception:
#         raise HTTPException(400, "Invalid webhook signature")
#
#     et = event.get("type")
#     obj = event.get("data", {}).get("object", {})
#
#     # We care about subscription being active / paid
#     if et in ("checkout.session.completed",):
#         # If you want: you can fetch subscription id from session and persist it
#         pass
#
#     if et in ("customer.subscription.created", "customer.subscription.updated"):
#         sub = obj
#         customer_id = sub.get("customer")
#         status = (sub.get("status") or "").lower()
#         sub_id = sub.get("id")
#
#         # Decide plan based on interval
#         items = (sub.get("items", {}).get("data") or [])
#         interval = None
#         if items and items[0].get("price") and items[0]["price"].get("recurring"):
#             interval = items[0]["price"]["recurring"].get("interval")  # "month" or "year"
#
#         db = SessionLocal()
#         try:
#             u = db.execute(select(User).where(User.stripe_customer_id == customer_id)).scalar_one_or_none()
#             if not u:
#                 return {"ok": True}
#
#             if status in ("active", "trialing"):
#                 u.plan = "pro_yearly" if interval == "year" else "pro_monthly"
#                 u.access_until = None
#                 u.stripe_subscription_id = sub_id
#             else:
#                 # canceled/unpaid/past_due -> lock back down
#                 u.plan = "free"
#                 u.access_until = None
#                 u.stripe_subscription_id = None
#
#             u.updated_at = datetime.utcnow()
#             db.commit()
#             return {"ok": True}
#         finally:
#             db.close()
#
#     if et in ("customer.subscription.deleted",):
#         customer_id = obj.get("customer")
#         db = SessionLocal()
#         try:
#             u = db.execute(select(User).where(User.stripe_customer_id == customer_id)).scalar_one_or_none()
#             if u:
#                 u.plan = "free"
#                 u.access_until = None
#                 u.stripe_subscription_id = None
#                 u.updated_at = datetime.utcnow()
#                 db.commit()
#         finally:
#             db.close()
#         return {"ok": True}
#
#     return {"ok": True}
