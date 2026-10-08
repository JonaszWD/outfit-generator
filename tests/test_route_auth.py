"""Every route needs a login unless it is on the explicit public list below.

Adding a route to `PUBLIC` is a deliberate act that shows up in review. Forgetting to protect a
new route fails this test, whichever router it lives in.
"""

import re
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from starlette.routing import Mount

from app.deps import get_current_user
from tests.conftest import form_token, new_client

PUBLIC: set[tuple[str, str]] = {
    ("GET", "/health"),
    ("GET", "/"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("POST", "/logout"),
    ("GET", "/register"),
    ("POST", "/register"),
    ("GET", "/forgot"),
    ("POST", "/forgot"),
    ("GET", "/reset/{token}"),
    ("POST", "/reset/{token}"),
}
PUBLIC_MOUNTS = {"/static"}


@dataclass(frozen=True)
class Endpoint:
    path: str
    dependant: Dependant


def routes(application: FastAPI) -> list[tuple[str, Endpoint]]:
    """(method, endpoint) for every route. Recent FastAPI includes routers lazily, so the
    effective route (with the include-level dependencies merged in) is read from the wrapper."""
    found: list[tuple[str, Endpoint]] = []
    for route in application.routes:
        if isinstance(route, Mount):
            assert route.path in PUBLIC_MOUNTS, f"unexpected mount {route.path}"
        elif isinstance(route, APIRoute):
            found.extend(
                (m, Endpoint(route.path, route.dependant)) for m in sorted(route.methods or ())
            )
        elif hasattr(route, "effective_route_contexts"):
            for effective in route.effective_route_contexts():
                endpoint = Endpoint(effective.path, effective.dependant)
                found.extend((m, endpoint) for m in sorted(effective.methods or ()))
        else:
            pytest.fail(f"unexpected route type {route!r}")
    return found


def depends_on_login(dependant: Dependant) -> bool:
    return dependant.call is get_current_user or any(
        depends_on_login(sub) for sub in dependant.dependencies
    )


def concrete(path: str) -> str:
    """Fill path parameters with values that match their converters."""
    return re.sub(r"\{[^}]+\}", "1", path)


def test_the_app_has_routes_to_check(auth_app: FastAPI) -> None:
    assert len(routes(auth_app)) > 30


def test_docs_and_openapi_are_not_exposed(auth_app: FastAPI) -> None:
    paths = {route.path for _, route in routes(auth_app)}
    assert not paths & {"/docs", "/redoc", "/openapi.json"}


def test_every_non_public_route_depends_on_get_current_user(auth_app: FastAPI) -> None:
    unprotected = [
        f"{method} {route.path}"
        for method, route in routes(auth_app)
        if (method, route.path) not in PUBLIC and not depends_on_login(route.dependant)
    ]
    assert unprotected == []


def test_the_public_list_has_no_stale_entries(auth_app: FastAPI) -> None:
    existing = {(method, route.path) for method, route in routes(auth_app)}
    assert existing >= PUBLIC


async def test_no_route_outside_the_public_list_works_without_a_session(
    auth_app: FastAPI,
) -> None:
    checked = 0
    async with new_client(auth_app) as anon:
        token = await form_token(anon)  # an anonymous CSRF token, so only auth can stop us
        for method, route in routes(auth_app):
            if (method, route.path) in PUBLIC:
                continue
            response = await anon.request(
                method,
                concrete(route.path),
                headers={"X-CSRF-Token": token, "Accept": "text/html"},
                data={"csrf_token": token} if method != "GET" else None,
            )
            assert response.status_code == 303, f"{method} {route.path}: {response.status_code}"
            assert response.headers["location"].startswith("/login"), f"{method} {route.path}"
            checked += 1
        assert checked > 20


async def test_htmx_and_json_callers_are_told_to_log_in(auth_app: FastAPI) -> None:
    async with new_client(auth_app) as anon:
        htmx = await anon.get("/closet", headers={"HX-Request": "true"})
        assert htmx.headers["HX-Redirect"].startswith("/login")
        api: httpx.Response = await anon.get("/closet", headers={"Accept": "application/json"})
        assert api.status_code == 401
