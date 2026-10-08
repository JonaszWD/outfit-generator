"""Security headers, request logging and error pages."""

import json
import logging
import time
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger("app.request")

# Scripts, styles and fonts: our own files, plus Google Fonts for typography. htmx is served
# from /static/vendor (no CDN script), inline scripts are not used (behavior lives in
# /static/app.js), and htmx is configured with allowEval=false and no inline indicator styles,
# so none of it needs 'unsafe-inline' or 'unsafe-eval'. The one exception is the `style=""`
# attribute (palette color swatches), allowed through style-src-attr only.
CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self' https://fonts.googleapis.com",
        "style-src-attr 'unsafe-inline'",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "object-src 'none'",
    ]
)

BASE_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def apply_security_headers(response: Response, hsts: bool = False) -> Response:
    for name, value in BASE_HEADERS.items():
        response.headers.setdefault(name, value)
    if hsts:
        hsts_value = "max-age=31536000; includeSubDomains"
        response.headers.setdefault("Strict-Transport-Security", hsts_value)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith(("text/html", "application/json")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, hsts: bool = False) -> None:
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        return apply_security_headers(await call_next(request), self.hsts)


class RequestLogMiddleware(BaseHTTPMiddleware):
    """One structured line per request: method, route template, status, duration, user id.

    The route *template* is logged (`/reset/{token}`), never the real path or the query
    string, and never an email, IP address or form value.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        logger.info(
            "request",
            extra={
                "fields": {
                    "method": request.method,
                    "route": getattr(route, "path", "unmatched"),
                    "status": response.status_code,
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                    "user_id": getattr(request.state, "user_id", None),
                }
            },
        )
        return response


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exception"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    root = logging.getLogger()
    if any(getattr(h, "_wardrobe", False) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler._wardrobe = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    if root.level in (logging.NOTSET, logging.WARNING):
        root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # it logs full URLs
