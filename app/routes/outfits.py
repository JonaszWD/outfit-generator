from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.csrf import require_csrf
from app.deps import CurrentUser, DbSession, EngineConfigDep, StorageDep, WeatherDep
from app.engine import vocab
from app.engine.config import EngineConfig
from app.engine.types import Context
from app.engine.weather import apply_weather, band_for, is_rainy
from app.models import Outfit
from app.services import feedback as feedback_svc
from app.services import outfits as svc
from app.services import style as style_svc
from app.services.preferences import get_preferences
from app.services.weather import Forecast, format_temperature, load_forecast
from app.templating import render

router = APIRouter()
CSRF = [Depends(require_csrf)]
SUGGESTIONS = 3


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _owned(outfit_id: int, session: DbSession, user: CurrentUser) -> Outfit:
    return svc.get_owned_outfit(session, user, outfit_id)


OwnedOutfit = Annotated[Outfit, Depends(_owned)]


@dataclass(frozen=True)
class WeatherStrip:
    """What the "Today's weather" strip shows."""

    temperature: str
    range: str
    band: str
    note: str
    rain: bool


def _strip(forecast: Forecast, config: EngineConfig, units: str) -> WeatherStrip | None:
    today = forecast.today
    if today is None:
        return None
    c = today.conditions
    low, high = format_temperature(c.temp_low_c, units), format_temperature(c.temp_high_c, units)
    band = band_for(c.representative_c, config.weather)
    return WeatherStrip(
        temperature=format_temperature(c.representative_c, units),
        range=f"{low} to {high}",
        band=band.name,
        note=band.note,
        rain=is_rainy(c, config.weather),
    )


@router.get("/outfits", response_class=HTMLResponse)
def outfits_page(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    config: EngineConfigDep,
    weather: WeatherDep,
    occasion: Annotated[str, Query()] = "",
) -> HTMLResponse:
    prefs = get_preferences(session, user)
    forecast = load_forecast(weather, prefs.latitude, prefs.longitude)
    saved_outfits = svc.list_saved(session, user, occasion or None)
    ratings = feedback_svc.ratings_for(session, user, saved_outfits)
    saved = svc.cards_for(session, user, saved_outfits, ratings)
    return render(
        request,
        "outfits.html",
        {
            "saved": saved,
            "storage": storage,
            "occasions": vocab.OCCASIONS,
            "seasons": vocab.SEASONS,
            "strip": _strip(forecast, config, prefs.units),
            "weather_notice": forecast.notice,
            "has_city": prefs.latitude is not None,
            "saved_filter": occasion if occasion in vocab.OCCASIONS else "",
            "style_refs": style_svc.list_references(session, user),
            "style_on": prefs.use_style_references,
        },
    )


@router.post("/outfits/generate", dependencies=CSRF, response_class=HTMLResponse)
def generate(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    config: EngineConfigDep,
    weather: WeatherDep,
    season: Annotated[str, Form()] = "",
    today: Annotated[str, Form()] = "",
    seen: Annotated[list[str] | None, Form()] = None,
    style_ai: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Three outfit cards for the chosen season. `seen` carries the outfits
    already shown, so Regenerate never repeats one. With `today`, the home city's forecast
    adds its temperature and rain constraints on top of the season."""
    context: Context = svc.parse_context(season or None)
    notice: str | None = None
    weather_note: str | None = None
    if today:
        prefs = get_preferences(session, user)
        forecast = load_forecast(weather, prefs.latitude, prefs.longitude)
        if forecast.today is not None:
            conditions = forecast.today.conditions
            context = apply_weather(context, conditions, config.weather)
            band = band_for(conditions.representative_c, config.weather)
            weather_note = (
                f"Dressed for {format_temperature(conditions.representative_c, prefs.units)}"
            )
            weather_note += ", rain likely. " if context.rain else ". "
            weather_note += band.note + "."
        else:
            notice = forecast.notice
    already = svc.parse_seen(seen or [])
    personalization = feedback_svc.personalization_for(session, user, config)
    # The reference-photo ranking is optional so it can be compared with the plain engine. Any
    # request without the checkbox ticked is "off"; the choice is remembered for the page.
    profile = style_svc.profile_for(session, user)
    use_style = bool(style_ai) and profile is not None
    if profile is not None:
        style_svc.remember_choice(session, user, bool(style_ai))
    result = svc.suggest(
        session,
        user,
        config,
        context,
        SUGGESTIONS,
        already,
        personalization=personalization,
        style=profile if use_style else None,
    )
    ratings = feedback_svc.ratings_for(session, user, result.outfits)
    cards = svc.cards_for(session, user, result.outfits, ratings)
    all_seen = sorted(
        {*(svc.base_key(c.outfit) for c in cards), *("-".join(map(str, k)) for k in already)}
    )
    return render(
        request,
        "_suggestions.html",
        {
            "cards": cards,
            "problems": result.problems,
            "exhausted": result.exhausted,
            "storage": storage,
            "season": context.season or "",
            "today": "1" if today else "",
            "notice": notice,
            "weather_note": weather_note,
            "seen": all_seen,
            "style_state": "" if profile is None else ("on" if use_style else "off"),
            "style_ai": "1" if use_style else "",
        },
    )


def _card_response(
    request: Request, session: DbSession, user: CurrentUser, storage: StorageDep, outfit: Outfit
) -> Response:
    if not _is_htmx(request):
        return RedirectResponse("/outfits", status_code=303)
    ratings = feedback_svc.ratings_for(session, user, [outfit])
    (card,) = svc.cards_for(session, user, [outfit], ratings)
    return render(request, "_outfit_card.html", {"card": card, "storage": storage})


@router.post("/outfits/{outfit_id:int}/save", dependencies=CSRF, response_model=None)
def save(
    request: Request,
    outfit: OwnedOutfit,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
) -> Response:
    try:
        svc.save_outfit(session, outfit)
    except svc.AlreadySaved:
        raise HTTPException(status_code=409, detail="This outfit is already saved.") from None
    return _card_response(request, session, user, storage, outfit)


@router.post("/outfits/{outfit_id:int}/occasions", dependencies=CSRF, response_model=None)
def occasions(
    request: Request,
    outfit: OwnedOutfit,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    tags: Annotated[list[str] | None, Form(alias="occasions")] = None,
) -> Response:
    """Occasion tags on a saved outfit."""
    if not outfit.favorite:
        raise HTTPException(status_code=409, detail="Save the outfit before tagging it.")
    svc.set_occasions(session, outfit, tags or [])
    return _card_response(request, session, user, storage, outfit)


@router.post("/outfits/{outfit_id:int}/feedback", dependencies=CSRF, response_model=None)
def give_feedback(
    request: Request,
    outfit: OwnedOutfit,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    rating: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    if rating not in ("up", "down", "clear"):
        raise HTTPException(status_code=422, detail="Choose like or dislike.")
    feedback_svc.record_feedback(session, user, outfit, rating, reason or None)
    return _card_response(request, session, user, storage, outfit)


@router.post("/outfits/{outfit_id:int}/delete", dependencies=CSRF, response_model=None)
def delete(request: Request, outfit: OwnedOutfit, session: DbSession) -> Response:
    svc.delete_outfit(session, outfit)
    if _is_htmx(request):
        return Response(status_code=200)  # the card swaps itself out for nothing
    return RedirectResponse("/outfits", status_code=303)
