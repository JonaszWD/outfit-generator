"""Privacy: download everything we hold about the user, or delete the account."""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.csrf import require_csrf
from app.deps import AppSettings, CurrentUser, DbSession, StorageDep
from app.routes.auth import TOO_MANY, _page, clear_session_cookie, client_ip
from app.services import account as svc
from app.services import auth
from app.services.passwords import verify_password

router = APIRouter()
CSRF = [Depends(require_csrf)]


@router.get("/account/export")
def export(session: DbSession, user: CurrentUser, storage: StorageDep) -> Response:
    """data.json plus every photo, in a zip. Only this user's rows and files."""
    archive = svc.export_zip(session, storage, user)
    return Response(
        archive.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="wardrobe-export.zip"',
            "Cache-Control": "no-store",
        },
    )


def _refuse(
    request: Request, session: DbSession, user: CurrentUser, message: str, status: int
) -> HTMLResponse:
    assert user.id is not None
    return _page(
        request,
        "account.html",
        status,
        user=user,
        sessions=auth.count_sessions(session, user.id),
        notice=None,
        error=message,
    )


@router.post("/account/delete", dependencies=CSRF, response_model=None)
def delete(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    settings: AppSettings,
    password: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    assert user.id is not None
    window = timedelta(minutes=settings.login_window_minutes)
    key = f"user:{user.id}"
    if auth.over_limit(session, "login_acct", key, settings.login_attempts_per_account, window):
        return _refuse(request, session, user, TOO_MANY, 429)
    if not verify_password(password, user.hashed_password):
        auth.record_attempt(session, "login_acct", key)
        auth.record_attempt(session, "login_ip", client_ip(request))
        return _refuse(request, session, user, "Your password is not right.", 401)
    if confirm.strip() != "DELETE":
        return _refuse(request, session, user, "Type DELETE to confirm.", 422)
    svc.delete_account(session, storage, user)
    response = RedirectResponse("/login?msg=deleted", status_code=303)
    clear_session_cookie(response)
    return response
