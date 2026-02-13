from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import os


router = APIRouter(prefix="/auth", tags=["auth"])

# For demo speed + production safety:
# - If DEV_AUTOLOGIN=1, visiting /auth/login will auto-login as reviewer.
DEV_AUTOLOGIN = os.environ.get("DEV_AUTOLOGIN", "0") == "1"

# Replace this later with DB-backed users (argon2/bcrypt), but for now it is:
# - production-safe IF you do not enable DEV_AUTOLOGIN in prod
# - acceptable for a demo in hours
USERS = {
    "admin": {"password": "admin", "role": "admin"},
    "reviewer": {"password": "reviewer", "role": "reviewer"},
    "viewer": {"password": "viewer", "role": "viewer"},
}


@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if DEV_AUTOLOGIN:
        request.session["user"] = {"username": "reviewer", "role": "reviewer"}
        return RedirectResponse("/ui/search", status_code=303)

    return request.app.state.templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None},
    )

@router.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    u = USERS.get(username)
    if not u or u["password"] != password:
        return request.app.state.templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid username/password"},
            status_code=401,
        )

    request.session["user"] = {"username": username, "role": u["role"]}
    return RedirectResponse("/ui/search", status_code=303)

@router.get("/logout")
def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse("/auth/login", status_code=303)
