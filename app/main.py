import logging
import os
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.types import Scope

from app.config import Settings, get_settings
from app.csrf import CSRFMiddleware
from app.deps import NotAuthenticated, get_current_user
from app.routes import (
    account,
    auth,
    candidates,
    closet,
    health,
    items,
    media,
    outfits,
    pages,
    planner,
    style,
)
from app.routes import (
    settings as settings_routes,
)
from app.security import (
    RequestLogMiddleware,
    SecurityHeadersMiddleware,
    apply_security_headers,
    configure_logging,
)
from app.templating import render

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_SECRET = "dev-only-secret-change-me"

# Public routers: no login needed. Everything else is registered with `PROTECTED` below, so a
# new router is private unless it is added to this short list on purpose.
PUBLIC_ROUTERS = (health, pages, auth)
PROTECTED = [Depends(get_current_user)]


class DevStaticFiles(StaticFiles):
    """Static files that the browser must revalidate on every load (cheap: an ETag check), so a
    CSS or JS edit shows up on the next refresh instead of after the browser's heuristic cache."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


async def _not_authenticated(request: Request, exc: Exception) -> Response:
    if request.headers.get("HX-Request") == "true":  # HTMX would swap the login page into a card
        return Response(status_code=200, headers={"HX-Redirect": "/login?msg=login"})
    if not _wants_html(request) and "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    target = "/login?msg=login"
    if request.method == "GET" and request.url.path != "/":
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        target += f"&next={quote(path, safe='')}"
    return RedirectResponse(target, status_code=303)


async def _http_error(request: Request, exc: HTTPException) -> Response:
    if _wants_html(request) and exc.status_code in (403, 404, 409, 410, 429):
        context = {"status": exc.status_code, "detail": exc.detail}
        return render(request, "error.html", context, exc.status_code)
    return await http_exception_handler(request, exc)


def _server_error_handler(hsts: bool):  # type: ignore[no-untyped-def]
    async def handler(request: Request, exc: Exception) -> Response:
        # Only the exception type is logged: messages can carry user data.
        logger.error("unhandled error", extra={"fields": {"error": type(exc).__name__}})
        if _wants_html(request):
            response: Response = render(request, "error.html", {"status": 500, "detail": ""}, 500)
        else:
            response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)
        return apply_security_headers(response, hsts)

    return handler


def production_problems(settings: Settings, serverless: bool) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a production configuration. Errors stop the app from starting:
    they would lose data or break the site. Warnings are logged."""
    if settings.env != "production":
        return [], []
    errors: list[str] = []
    warnings: list[str] = []
    if serverless and settings.storage_backend == "local":
        errors.append(
            "Photos would be written to a disk that disappears: set STORAGE_BACKEND to "
            "supabase or s3 on a serverless host."
        )
    if serverless and settings.cutout_backend == "rembg":
        errors.append("Set CUTOUT_BACKEND=none on a serverless host (rembg does not fit).")
    if settings.tagger_backend == "fake":
        warnings.append("TAGGER_BACKEND=fake: new items get placeholder tags, not real ones.")
    if settings.email_backend == "console":
        warnings.append("EMAIL_BACKEND=console: password-reset links are written to the log.")
    if "localhost" in settings.app_base_url or "127.0.0.1" in settings.app_base_url:
        warnings.append("APP_BASE_URL points at localhost: reset links will not work.")
    if serverless and not settings.db_serverless:
        warnings.append("DB_SERVERLESS is off: every function instance keeps its own connections.")
    if serverless and not settings.trust_proxy_headers:
        warnings.append("TRUST_PROXY_HEADERS is off: all visitors share one rate-limit address.")
    return errors, warnings


def create_app() -> FastAPI:
    settings = get_settings()
    secret = settings.secret_key.get_secret_value()
    if settings.env == "production" and secret == DEFAULT_SECRET:
        raise RuntimeError("SECRET_KEY must be set to a private value when ENV=production")
    configure_logging()
    errors, warnings = production_problems(settings, serverless=bool(os.environ.get("VERCEL")))
    if errors:
        raise RuntimeError("Unsafe production configuration: " + " ".join(errors))
    for warning in warnings:
        logger.warning(warning)
    app = FastAPI(title="Wardrobe", docs_url=None, redoc_url=None, openapi_url=None)
    # Added innermost first: CSRF, then request logging, then security headers outermost.
    app.add_middleware(CSRFMiddleware, secret=secret, secure=settings.secure_cookies)
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.secure_cookies)
    app.add_exception_handler(NotAuthenticated, _not_authenticated)
    app.add_exception_handler(HTTPException, _http_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _server_error_handler(settings.secure_cookies))
    static = DevStaticFiles if settings.env != "production" else StaticFiles
    app.mount("/static", static(directory=STATIC_DIR), name="static")
    for module in PUBLIC_ROUTERS:
        app.include_router(module.router)
    for module in (
        closet,
        items,
        outfits,
        candidates,
        media,
        planner,
        settings_routes,
        account,
        style,
    ):
        app.include_router(module.router, dependencies=PROTECTED)
    app.include_router(auth.account_router, dependencies=PROTECTED)
    return app
