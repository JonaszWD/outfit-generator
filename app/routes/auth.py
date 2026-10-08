"""Registration, login, logout, password reset and the account page."""

import secrets
from datetime import timedelta
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.config import Settings
from app.csrf import require_csrf
from app.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    EmailSenderDep,
    OptionalUser,
)
from app.models import User
from app.services import auth
from app.services.mailer import send_in_background
from app.services.passwords import (
    normalize_email,
    valid_email,
    validate_password,
    verify_password,
)
from app.templating import render

router = APIRouter()
account_router = APIRouter()  # requires a session (see create_app)
CSRF = [Depends(require_csrf)]

MESSAGES = {
    "registered": "Thanks! If that email can be used, your account is ready. You can log in now.",
    "reset": "Password updated. Log in with your new password.",
    "loggedout": "You are logged out.",
    "login": "Please log in to continue.",
    "deleted": "Your account and all its data were deleted.",
    "sent": "If an account exists for that email, we have sent a link to reset the password.",
}
TOO_MANY = "Too many attempts. Please wait a few minutes and try again."


def client_ip(request: Request, trust_proxy: bool = False) -> str:
    """The caller's address. Behind a proxy that sets X-Forwarded-For (Vercel does, and
    replaces whatever the client sent), every request would otherwise share the proxy's
    address and the per-IP rate limits would lock everyone out together."""
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


def safe_next(target: str | None) -> str:
    """Only same-site paths: never an absolute URL, `//host`, or a backslash trick."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/closet"
    return target


def set_session_cookie(response: Response, token: str, settings: AppSettings) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(auth.SESSION_COOKIE, path="/")


def _page(
    request: Request, template: str, status_code: int = 200, **context: object
) -> HTMLResponse:
    return render(request, template, context, status_code=status_code)


def _login_page(
    request: Request,
    status_code: int = 200,
    *,
    error: str | None = None,
    message: str | None = None,
    next: str = "",
    email: str = "",
) -> HTMLResponse:
    return _page(
        request,
        "login.html",
        status_code,
        error=error,
        message=message,
        next=safe_next(next),
        email=email,
    )


def _register_page(
    request: Request,
    settings: Settings,
    status_code: int = 200,
    *,
    error: str | None = None,
    email: str = "",
) -> HTMLResponse:
    min_length = settings.min_password_length
    return _page(
        request,
        "register.html",
        status_code,
        error=error,
        email=email,
        min_length=min_length,
        closed=not settings.registration_allowed,
        needs_invite=settings.invite_code is not None,
    )


def _reset_page(
    request: Request,
    settings: Settings,
    token: str,
    status_code: int = 200,
    *,
    valid: bool = True,
    error: str | None = None,
) -> HTMLResponse:
    min_length = settings.min_password_length
    return _page(
        request,
        "reset.html",
        status_code,
        token=token,
        valid=valid,
        error=error,
        min_length=min_length,
    )


# ------------------------------------------------------------------------------ login


@router.get("/login", response_class=HTMLResponse, response_model=None)
def login_form(request: Request, user: OptionalUser, msg: str = "", next: str = "") -> Response:
    if user is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return _login_page(request, message=MESSAGES.get(msg), next=next)


@router.post("/login", dependencies=CSRF, response_model=None)
def login(
    request: Request,
    session: DbSession,
    settings: AppSettings,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
) -> Response:
    email = normalize_email(email)
    ip = client_ip(request, settings.trust_proxy_headers)
    window = timedelta(minutes=settings.login_window_minutes)
    ip_limit, account_limit = settings.login_attempts_per_ip, settings.login_attempts_per_account
    if auth.over_limit(session, "login_ip", ip, ip_limit, window) or auth.over_limit(
        session, "login_acct", email, account_limit, window
    ):
        limited = _login_page(request, 429, error=TOO_MANY, next=next, email=email)
        limited.headers["Retry-After"] = str(int(window.total_seconds()))
        return limited
    user = auth.authenticate(session, email, password)
    if user is None:
        auth.record_attempt(session, "login_ip", ip)
        auth.record_attempt(session, "login_acct", email)
        return _login_page(
            request, 401, error="Incorrect email or password.", next=next, email=email
        )
    auth.clear_attempts(session, "login_acct", email)
    old = request.cookies.get(auth.SESSION_COOKIE)
    if old:  # rotate: the presented session never survives a login
        auth.revoke_session(session, old)
    token = auth.create_session(session, user, settings)
    response = RedirectResponse(safe_next(next), status_code=303)
    set_session_cookie(response, token, settings)
    return response


@router.post("/logout", dependencies=CSRF, response_model=None)
def logout(request: Request, session: DbSession) -> Response:
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        auth.revoke_session(session, token)
    response = RedirectResponse("/login?msg=loggedout", status_code=303)
    clear_session_cookie(response)
    return response


# --------------------------------------------------------------------------- register


@router.get("/register", response_class=HTMLResponse, response_model=None)
def register_form(request: Request, user: OptionalUser, settings: AppSettings) -> Response:
    if user is not None:
        return RedirectResponse("/closet", status_code=303)
    return _register_page(request, settings)


@router.post("/register", dependencies=CSRF, response_model=None)
async def register(
    request: Request,
    session: DbSession,
    settings: AppSettings,
    sender: EmailSenderDep,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    invite: Annotated[str, Form()] = "",
) -> Response:
    """Always ends in the same redirect, whether or not the email was already taken."""
    email = normalize_email(email)
    if not settings.registration_allowed:
        return _register_page(request, settings, 403, email=email)
    ip = client_ip(request, settings.trust_proxy_headers)
    if auth.over_limit(session, "register_ip", ip, settings.register_per_ip, timedelta(hours=1)):
        return _register_page(request, settings, 429, error=TOO_MANY, email=email)
    auth.record_attempt(session, "register_ip", ip)
    problem = None
    if settings.invite_code is not None and not secrets.compare_digest(
        invite.strip().encode(), settings.invite_code.get_secret_value().encode()
    ):
        problem = "That invite code is not valid."
    elif not valid_email(email):
        problem = "Enter a valid email address."
    else:
        problem = validate_password(password, email, settings.min_password_length)
    if problem:
        return _register_page(request, settings, 422, error=problem, email=email)
    created = auth.create_user(session, email, password)
    if created is None:  # already registered: say nothing, but tell the owner
        await send_in_background(
            sender,
            email,
            "Someone tried to register with your email",
            "Someone tried to create a Wardrobe account with this email address, but you "
            "already have one. If that was you, just log in. If you forgot your password, "
            f"you can reset it at {settings.app_base_url}/forgot.\n\nIf it was not you, "
            "you can ignore this message.",
        )
    return RedirectResponse("/login?msg=registered", status_code=303)


# ------------------------------------------------------------------- password reset


@router.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request) -> HTMLResponse:
    return _page(request, "forgot.html", message=None, error=None)


@router.post("/forgot", dependencies=CSRF, response_model=None)
async def forgot(
    request: Request,
    session: DbSession,
    settings: AppSettings,
    sender: EmailSenderDep,
    email: Annotated[str, Form()] = "",
) -> Response:
    """The same answer for every email, so this cannot be used to find out who has an account."""
    email = normalize_email(email)
    ip = client_ip(request, settings.trust_proxy_headers)
    hour = timedelta(hours=1)
    if auth.over_limit(session, "reset_ip", ip, settings.reset_requests_per_ip, hour):
        return _page(request, "forgot.html", 429, message=None, error=TOO_MANY)
    auth.record_attempt(session, "reset_ip", ip)
    per_account = settings.reset_requests_per_account
    limited = auth.over_limit(session, "reset_acct", email, per_account, hour)
    auth.record_attempt(session, "reset_acct", email)
    user = auth.find_user(session, email) if valid_email(email) else None
    if user is not None and not limited:
        token = auth.create_reset_token(session, user)
        await send_in_background(
            sender,
            email,
            "Reset your Wardrobe password",
            f"Use this link within one hour to choose a new password:\n\n"
            f"{settings.app_base_url}/reset/{quote(token)}\n\n"
            "It works once. If you did not ask for this, you can ignore this message.",
        )
    return _page(request, "forgot.html", message=MESSAGES["sent"], error=None)


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_form(
    request: Request, token: str, session: DbSession, settings: AppSettings
) -> HTMLResponse:
    valid = auth.reset_token_valid(session, token)
    return _reset_page(request, settings, token, 200 if valid else 410, valid=valid)


@router.post("/reset/{token}", dependencies=CSRF, response_model=None)
def reset(
    request: Request,
    token: str,
    session: DbSession,
    settings: AppSettings,
    password: Annotated[str, Form()] = "",
) -> Response:
    if not auth.reset_token_valid(session, token):
        return _reset_page(request, settings, token, 410, valid=False)
    owner = auth.reset_token_owner(session, token)  # the email is needed for the policy check
    email = owner.email if owner else ""
    problem = validate_password(password, email, settings.min_password_length)
    if problem:
        return _reset_page(request, settings, token, 422, error=problem)
    user = auth.consume_reset_token(session, token)  # single use, race-safe
    if user is None:
        return _reset_page(request, settings, token, 410, valid=False)
    auth.set_password(session, user, password)
    assert user.id is not None
    auth.revoke_all_sessions(session, user.id)  # whoever had access loses it
    return RedirectResponse("/login?msg=reset", status_code=303)


# --------------------------------------------------------------------------- account


@account_router.get("/account", response_class=HTMLResponse)
def account(
    request: Request, user: CurrentUser, session: DbSession, ok: str = Query(default="")
) -> HTMLResponse:
    assert user.id is not None
    notice = {"password": "Password changed.", "others": "Logged out everywhere else."}.get(ok)
    return _page(
        request,
        "account.html",
        user=user,
        sessions=auth.count_sessions(session, user.id),
        notice=notice,
        error=None,
    )


@account_router.post("/account/password", dependencies=CSRF, response_model=None)
def change_password(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
) -> Response:
    assert user.id is not None
    ip = client_ip(request, settings.trust_proxy_headers)
    window = timedelta(minutes=settings.login_window_minutes)
    key = f"user:{user.id}"
    if auth.over_limit(session, "login_acct", key, settings.login_attempts_per_account, window):
        return _account_error(request, session, user, TOO_MANY, 429)
    if not verify_password(current_password, user.hashed_password):
        auth.record_attempt(session, "login_acct", key)
        auth.record_attempt(session, "login_ip", ip)
        return _account_error(request, session, user, "Your current password is not right.", 401)
    problem = validate_password(new_password, user.email, settings.min_password_length)
    if problem:
        return _account_error(request, session, user, problem, 422)
    auth.set_password(session, user, new_password)
    auth.revoke_all_sessions(session, user.id)  # every device, including this one...
    token = auth.create_session(session, user, settings)  # ...then this one is logged in afresh
    response = RedirectResponse("/account?ok=password", status_code=303)
    set_session_cookie(response, token, settings)
    return response


@account_router.post("/account/logout-everywhere", dependencies=CSRF, response_model=None)
def logout_everywhere(user: CurrentUser, session: DbSession, settings: AppSettings) -> Response:
    assert user.id is not None
    auth.revoke_all_sessions(session, user.id)
    response = RedirectResponse("/login?msg=loggedout", status_code=303)
    clear_session_cookie(response)
    return response


def _account_error(
    request: Request, session: DbSession, user: User, message: str, status_code: int
) -> HTMLResponse:
    assert user.id is not None
    return _page(
        request,
        "account.html",
        status_code,
        user=user,
        sessions=auth.count_sessions(session, user.id),
        notice=None,
        error=message,
    )
