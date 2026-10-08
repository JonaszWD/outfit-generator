"""The weekly planner: saved outfits on days, with warnings."""

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.csrf import require_csrf
from app.deps import CurrentUser, DbSession, EngineConfigDep, StorageDep, WeatherDep
from app.services import outfits as outfit_svc
from app.services import planner as svc
from app.services.preferences import get_preferences
from app.services.weather import load_forecast
from app.templating import render

router = APIRouter()
CSRF = [Depends(require_csrf)]


def _back(start: date) -> RedirectResponse:
    return RedirectResponse(f"/planner?start={start.isoformat()}", status_code=303)


@router.get("/planner", response_class=HTMLResponse)
def planner_page(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    config: EngineConfigDep,
    weather: WeatherDep,
    start: Annotated[str, Query()] = "",
) -> HTMLResponse:
    first = svc.week_start(svc.parse_day(start, date.today()) or date.today())
    prefs = get_preferences(session, user)
    forecast = load_forecast(weather, prefs.latitude, prefs.longitude)
    plan = svc.plan_week(session, user, first, forecast, config, prefs.units)
    saved = outfit_svc.cards_for(session, user, outfit_svc.list_saved(session, user))
    return render(
        request,
        "planner.html",
        {
            "plan": plan,
            "saved": saved,
            "storage": storage,
            "start": first,
            "previous": first - timedelta(days=7),
            "next": first + timedelta(days=7),
            "weather_notice": forecast.notice,
        },
    )


@router.post("/planner/assign", dependencies=CSRF, response_model=None)
def assign(
    session: DbSession,
    user: CurrentUser,
    day: Annotated[str, Form()] = "",
    outfit_id: Annotated[int, Form()] = 0,
) -> RedirectResponse:
    target = svc.parse_day(day)
    if target is None:
        return _back(date.today())
    outfit = outfit_svc.get_owned_outfit(session, user, outfit_id)
    if not outfit.favorite:  # only saved outfits can be planned
        return _back(svc.week_start(target))
    svc.assign(session, user, target, outfit)
    return _back(svc.week_start(target))


@router.post("/planner/clear", dependencies=CSRF, response_model=None)
def clear(
    session: DbSession, user: CurrentUser, day: Annotated[str, Form()] = ""
) -> RedirectResponse:
    target = svc.parse_day(day)
    if target is None:
        return _back(date.today())
    svc.clear(session, user, target)
    return _back(svc.week_start(target))
