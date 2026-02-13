# app/auth/deps.py
from __future__ import annotations

from datetime import datetime, timezone
from fastapi import Request, HTTPException, Depends
from typing import Callable


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# def require_user(request: Request):
#     user = request.session.get("user")
#     if not user:
#         raise HTTPException(status_code=401, detail="Not authenticated")
#     if user.get("is_active") is False:
#         raise HTTPException(status_code=403, detail="User disabled")
#     return user

def require_user(request: Request):
    user = request.session.get("user")

    if not user:
        # If it's a browser page load, redirect instead of returning JSON.
        accept = (request.headers.get("accept") or "").lower()
        if "text/html" in accept:
            # 303 redirect to login page
            raise HTTPException(status_code=303, headers={"Location": "/auth/login"})
        raise HTTPException(status_code=401, detail="Not authenticated")

    if user.get("is_active") is False:
        raise HTTPException(status_code=403, detail="User disabled")

    return user

def require_role(*roles: str) -> Callable:
    def _dep(user=Depends(require_user)):
        if user.get("role") not in set(roles):
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user
    return _dep


def require_paid_user(user=Depends(require_user)):
    # Admin bypass (optional but convenient)
    if user.get("role") == "admin":
        return user

    plan = (user.get("plan") or "free").lower()
    if plan == "free":
        raise HTTPException(status_code=402, detail="Access Code Required")

    access_until = user.get("access_until")
    if access_until:
        try:
            dt = datetime.fromisoformat(access_until)
        except Exception:
            dt = None
        if dt and dt <= _now_utc():
            raise HTTPException(status_code=402, detail="Access expired")

    return user
