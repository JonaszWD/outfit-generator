"""Preferences (home city, strictness, units) and personalization."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.csrf import require_csrf
from app.deps import CurrentUser, DbSession, EngineConfigDep, WeatherDep
from app.services import feedback as feedback_svc
from app.services import preferences as svc
from app.services.weather import Place
from app.templating import render

router = APIRouter()
CSRF = [Depends(require_csrf)]
MESSAGES = {
    "saved": "Preferences saved.",
    "city": "Home city saved.",
    "nocity": "Home city removed.",
    "reset": "Personalization reset. Suggestions are back to the defaults.",
}


def _page(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    config: EngineConfigDep,
    status_code: int = 200,
    **extra: object,
) -> HTMLResponse:
    context = {
        "prefs": svc.get_preferences(session, user),
        "summary": feedback_svc.summarize(session, user, config),
        "errors": {},
        "notice": None,
        "places": None,
        "query": "",
        "city_error": None,
        **extra,
    }
    return render(request, "preferences.html", context, status_code=status_code)


@router.get("/settings/preferences", response_class=HTMLResponse)
def preferences_page(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    config: EngineConfigDep,
    ok: str = "",
) -> HTMLResponse:
    return _page(request, session, user, config, notice=MESSAGES.get(ok))


@router.post("/settings/preferences", dependencies=CSRF, response_model=None)
def save_preferences(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    config: EngineConfigDep,
    color_strictness: Annotated[str, Form()] = "0.5",
    units: Annotated[str, Form()] = "metric",
) -> Response:
    data, errors = svc.parse_preferences(color_strictness, units)
    if data is None:
        return _page(request, session, user, config, 422, errors=errors)
    svc.save_preferences(session, user, data)
    return RedirectResponse("/settings/preferences?ok=saved", status_code=303)


@router.post("/settings/city/search", dependencies=CSRF, response_class=HTMLResponse)
def search_city(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    config: EngineConfigDep,
    weather: WeatherDep,
    q: Annotated[str, Form()] = "",
) -> HTMLResponse:
    places, ok = svc.search_places(weather, q)
    error = None
    if not ok:
        error = "City search is unavailable right now. Try again later."
    elif not places:
        error = "No city found with that name."
    return _page(
        request,
        session,
        user,
        config,
        200 if error is None else 422 if ok else 503,
        places=places,
        query=q,
        city_error=error,
    )


@router.post("/settings/city", dependencies=CSRF, response_model=None)
def choose_city(
    session: DbSession,
    user: CurrentUser,
    name: Annotated[str, Form()] = "",
    latitude: Annotated[float, Form(ge=-90, le=90)] = 0.0,
    longitude: Annotated[float, Form(ge=-180, le=180)] = 0.0,
    clear: Annotated[str, Form()] = "",
) -> RedirectResponse:
    if clear:
        svc.set_home(session, user, None)
        return RedirectResponse("/settings/preferences?ok=nocity", status_code=303)
    svc.set_home(session, user, Place(name.strip(), latitude, longitude))
    return RedirectResponse("/settings/preferences?ok=city", status_code=303)


@router.post("/settings/personalization/reset", dependencies=CSRF, response_model=None)
def reset_personalization(session: DbSession, user: CurrentUser) -> RedirectResponse:
    feedback_svc.reset_personalization(session, user)
    return RedirectResponse("/settings/preferences?ok=reset", status_code=303)
