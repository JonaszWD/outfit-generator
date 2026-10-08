"""CSRF protection that works with sessions and HTMX.

Logged in: the token is an HMAC of the session cookie (keyed with SECRET_KEY), so it is bound
to that login, needs no storage, and a token from another session never validates.
Logged out (login, registration, password reset forms): a double-submit cookie with a random
token, which also covers login CSRF.

Either way the token travels in a hidden form field, or in the X-CSRF-Token header, which HTMX
adds to every request (see base.html). Unsafe requests must send it.
"""

import hashlib
import hmac
import re
import secrets

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

from app.services.auth import SESSION_COOKIE

COOKIE_NAME = "csrf_token"
FORM_FIELD = "csrf_token"
HEADER_NAME = "X-CSRF-Token"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


def session_csrf_token(secret: str, session_cookie: str) -> str:
    return hmac.new(secret.encode(), f"csrf:{session_cookie}".encode(), hashlib.sha256).hexdigest()


class CSRFMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, secret: str, secure: bool = False) -> None:
        super().__init__(app)
        self.secret = secret
        self.secure = secure

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        session_cookie = request.cookies.get(SESSION_COOKIE)
        if session_cookie and _TOKEN_RE.match(session_cookie):
            request.state.csrf_mode = "session"
            request.state.csrf_token = session_csrf_token(self.secret, session_cookie)
            return await call_next(request)
        existing = request.cookies.get(COOKIE_NAME)
        token = existing if existing and _TOKEN_RE.match(existing) else secrets.token_urlsafe(32)
        request.state.csrf_mode = "anonymous"
        request.state.csrf_token = token
        response = await call_next(request)
        if token != existing:
            response.set_cookie(
                COOKIE_NAME, token, httponly=True, samesite="lax", secure=self.secure
            )
        return response


async def require_csrf(request: Request) -> None:
    supplied = request.headers.get(HEADER_NAME)
    if not supplied:
        form = await request.form()
        value = form.get(FORM_FIELD)
        supplied = value if isinstance(value, str) else None
    if request.state.csrf_mode == "session":
        expected = request.state.csrf_token
    else:
        expected = request.cookies.get(COOKIE_NAME)
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
