"""Preferences: validation, per-user engine configuration, and the home city."""

import httpx
from sqlmodel import Session

from app.engine.config import EngineConfig
from app.models import User, UserPreferences
from app.services.preferences import engine_config_for, get_preferences
from app.services.weather import FakeWeatherProvider
from tests.conftest import post_form


def test_defaults_reproduce_the_engine_configuration_exactly(
    session: Session, user_a: User
) -> None:
    assert engine_config_for(session, user_a) == EngineConfig()
    session.add(UserPreferences(user_id=user_a.id or 0))  # strictness 0.5
    session.flush()
    assert engine_config_for(session, user_a) == EngineConfig()


def test_strictness_changes_only_that_users_config(
    session: Session, user_a: User, user_b: User
) -> None:
    session.add(
        UserPreferences(
            user_id=user_a.id or 0,
            color_strictness=1.0,
        )
    )
    session.flush()
    tuned = engine_config_for(session, user_a)
    assert tuned.max_non_neutral_colors == 1
    assert engine_config_for(session, user_b) == EngineConfig()


async def test_preferences_page_lists_every_control_with_a_label(
    client: httpx.AsyncClient,
) -> None:
    html = (await client.get("/settings/preferences")).text
    for field in ("city-q", "color_strictness", "units"):
        assert f'for="{field}"' in html and f'id="{field}"' in html


async def test_saving_preferences(
    client: httpx.AsyncClient, session: Session, user_a: User
) -> None:
    response = await post_form(
        client,
        "/settings/preferences",
        {
            "color_strictness": "0.8",
            "units": "imperial",
        },
    )
    assert response.status_code == 303
    prefs = get_preferences(session, user_a)
    assert prefs.units == "imperial" and prefs.color_strictness == 0.8


async def test_invalid_preferences_are_refused_and_nothing_is_saved(
    client: httpx.AsyncClient, session: Session, user_a: User
) -> None:
    for bad in (
        {"color_strictness": "2", "units": "metric"},
        {"color_strictness": "x", "units": "metric"},
        {"color_strictness": "0.5", "units": "kelvin"},
    ):
        response = await post_form(client, "/settings/preferences", bad)
        assert response.status_code == 422 and 'role="alert"' in response.text
    assert session.get(UserPreferences, user_a.id) is None


async def test_choosing_a_city_stores_rounded_coordinates(
    client: httpx.AsyncClient, session: Session, user_a: User
) -> None:
    search = await post_form(client, "/settings/city/search", {"q": "oslo"})
    assert search.status_code == 200 and "Oslo, Norway" in search.text
    chosen = await post_form(
        client,
        "/settings/city",
        {"name": "Oslo, Norway", "latitude": "59.913868", "longitude": "10.752245"},
    )
    assert chosen.status_code == 303
    prefs = get_preferences(session, user_a)
    assert (prefs.home_city, prefs.latitude, prefs.longitude) == ("Oslo, Norway", 59.91, 10.75)
    removed = await post_form(client, "/settings/city", {"clear": "1"})
    assert removed.status_code == 303
    assert get_preferences(session, user_a).latitude is None


async def test_city_search_failures_are_explained(
    client: httpx.AsyncClient, weather: FakeWeatherProvider
) -> None:
    missing = await post_form(client, "/settings/city/search", {"q": "Atlantis"})
    assert missing.status_code == 422 and "No city found" in missing.text
    weather.fail = True
    down = await post_form(client, "/settings/city/search", {"q": "oslo"})
    assert down.status_code == 503 and "unavailable" in down.text


async def test_out_of_range_coordinates_are_rejected(client: httpx.AsyncClient) -> None:
    response = await post_form(
        client, "/settings/city", {"name": "x", "latitude": "91", "longitude": "0"}
    )
    assert response.status_code == 422
