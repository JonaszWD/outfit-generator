"""Weather providers, the forecast cache, and "Dress me for today" end to end."""

import json
import re
from datetime import date

import httpx
import pytest
from sqlmodel import Session

from app.engine.weather import Conditions
from app.models import Category, User, UserPreferences
from app.services.storage import LocalDiskStorage
from app.services.weather import (
    CachedWeather,
    FakeWeatherProvider,
    OpenMeteoProvider,
    Place,
    WeatherError,
    format_temperature,
    load_forecast,
    round_coordinate,
)
from tests.conftest import post_form
from tests.test_outfit_routes import closet, piece

# ------------------------------------------------------------------------ providers


def open_meteo(handler: httpx.MockTransport) -> OpenMeteoProvider:
    return OpenMeteoProvider(client=httpx.Client(transport=handler))


FORECAST_JSON = {
    "daily": {
        "time": ["2026-10-06", "2026-10-07"],
        "temperature_2m_max": [14.0, 9.5],
        "temperature_2m_min": [6.0, 3.0],
        "precipitation_probability_max": [20, None],
        "precipitation_sum": [0.0, 2.4],
    }
}


def test_open_meteo_parses_the_forecast_and_sends_rounded_coordinates() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=FORECAST_JSON)

    days = open_meteo(httpx.MockTransport(handler)).forecast(59.913868, 10.752245)
    assert [d.day for d in days] == [date(2026, 10, 6), date(2026, 10, 7)]
    assert days[0].conditions == Conditions(14.0, 6.0, 20.0, 0.0)
    assert days[1].conditions.rain_probability_percent == 0.0  # null means none
    assert days[1].conditions.rain_millimetres == 2.4
    (request,) = seen
    assert request.url.host == "api.open-meteo.com"
    assert request.url.params["latitude"] == "59.91" and request.url.params["longitude"] == "10.75"
    assert set(request.url.params) == {
        "latitude",
        "longitude",
        "daily",
        "forecast_days",
        "timezone",
    }  # nothing about the user besides rounded coordinates


def test_open_meteo_geocoding_returns_rounded_places() -> None:
    payload = {
        "results": [
            {
                "name": "Oslo",
                "admin1": "Oslo",
                "country": "Norway",
                "latitude": 59.91273,
                "longitude": 10.74609,
            },
            {"name": "broken"},
        ]
    }
    provider = open_meteo(httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    assert provider.geocode("Oslo") == [Place("Oslo, Oslo, Norway", 59.91, 10.75)]
    assert provider.geocode("   ") == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"daily": {"time": []}}),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, content=json.dumps([1, 2]).encode()),
    ],
)
def test_open_meteo_failures_become_weather_errors(response: httpx.Response) -> None:
    provider = open_meteo(httpx.MockTransport(lambda r: response))
    with pytest.raises(WeatherError):
        provider.forecast(1.0, 2.0)


def test_open_meteo_network_error_becomes_weather_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(WeatherError):
        open_meteo(httpx.MockTransport(boom)).forecast(1.0, 2.0)


def test_coordinates_round_to_two_decimals() -> None:
    assert round_coordinate(59.916) == 59.92 and round_coordinate(-0.004) == 0.0


def test_temperature_units() -> None:
    assert format_temperature(20.0, "metric") == "20°C"
    assert format_temperature(20.0, "imperial") == "68°F"
    assert format_temperature(-5.0, "imperial") == "23°F"


# ---------------------------------------------------------------------------- cache


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_forecast_is_cached_for_an_hour_per_rounded_location() -> None:
    fake, clock = FakeWeatherProvider(), Clock()
    cached = CachedWeather(fake, 3600.0, clock)
    cached.forecast(59.911, 10.751)
    cached.forecast(59.912, 10.749)  # the same place once rounded
    assert fake.calls == 1
    cached.forecast(60.5, 5.3)  # another place
    assert fake.calls == 2
    clock.now += 3599
    cached.forecast(59.91, 10.75)
    assert fake.calls == 2
    clock.now += 2
    cached.forecast(59.91, 10.75)
    assert fake.calls == 3
    assert fake.requested[0] == (59.91, 10.75)  # the provider only ever sees rounded values


def test_failures_are_remembered_briefly_so_a_dead_api_is_not_hammered() -> None:
    fake, clock = FakeWeatherProvider(fail=True), Clock()
    cached = CachedWeather(fake, 3600.0, clock)
    for _ in range(3):
        with pytest.raises(WeatherError):
            cached.forecast(1.0, 2.0)
    assert fake.calls == 1
    clock.now += CachedWeather.FAILURE_TTL + 1
    fake.fail = False
    assert cached.forecast(1.0, 2.0)
    assert fake.calls == 2


def test_load_forecast_falls_back_with_a_notice() -> None:
    assert "home city" in (load_forecast(FakeWeatherProvider(), None, None).notice or "")
    down = load_forecast(FakeWeatherProvider(fail=True), 1.0, 2.0)
    assert down.days == [] and "Choose a season" in (down.notice or "")
    ok = load_forecast(FakeWeatherProvider(), 1.0, 2.0)
    assert len(ok.days) == 7 and ok.notice is None and ok.today is not None


# ----------------------------------------------------------------------------- routes


def set_home(session: Session, user: User, units: str = "metric") -> None:
    session.add(UserPreferences(user_id=user.id or 0, latitude=59.91, longitude=10.75, units=units))
    session.flush()


def alts(html: str) -> list[list[str]]:
    """Per card, the item names (alt text without the colour)."""
    cards = html.split('class="card outfit"')[1:]
    return [
        [a.split(",")[0] for a in re.findall(r'alt="([^"]*)"', c.split("</article>")[0])]
        for c in cards
    ]


async def test_outfit_page_shows_todays_weather_and_the_one_click_button(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    weather: FakeWeatherProvider,
) -> None:
    set_home(session, user_a)
    weather.conditions = Conditions(16.0, 8.0, rain_probability_percent=70.0)
    page = (await client.get("/outfits")).text
    assert "Today&#39;s weather" in page or "Today's weather" in page
    assert "12°C" in page and "8°C to 16°C" in page and "Rain likely" in page
    assert "Dress me for today" in page
    assert weather.requested == [(59.91, 10.75)]


async def test_fahrenheit_users_see_fahrenheit(
    client: httpx.AsyncClient, session: Session, user_a: User, weather: FakeWeatherProvider
) -> None:
    set_home(session, user_a, units="imperial")
    weather.conditions = Conditions(20.0, 20.0)
    assert "68°F" in (await client.get("/outfits")).text


async def test_page_without_a_city_asks_for_one(client: httpx.AsyncClient) -> None:
    page = (await client.get("/outfits")).text
    assert "Set your home city" in page and "Dress me for today" not in page
    assert "/settings/preferences" in page


async def test_dress_me_for_today_in_the_cold_always_includes_outerwear(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    weather: FakeWeatherProvider,
) -> None:
    closet(session, storage, user_a)
    set_home(session, user_a)
    weather.conditions = Conditions(3.0, 1.0)
    response = await post_form(client, "/outfits/generate", {"today": "1"})
    cards = alts(response.text)
    assert cards and all("jacket" in names for names in cards)
    assert "Dressed for 2°C" in response.text and "warm layer" in response.text
    assert 'name="today" value="1"' in response.text  # Regenerate keeps the weather


async def test_rain_rules_out_sandals_and_prefers_a_jacket(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    weather: FakeWeatherProvider,
) -> None:
    closet(session, storage, user_a)
    piece(session, storage, user_a, Category.SHOES, "sandals", "tan", "Sandals")
    set_home(session, user_a)
    weather.conditions = Conditions(19.0, 17.0, rain_probability_percent=90.0)
    response = await post_form(client, "/outfits/generate", {"today": "1"})
    assert "rain likely" in response.text
    assert all("Sandals" not in names for names in alts(response.text))


async def test_weather_and_season_filters_combine(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    weather: FakeWeatherProvider,
) -> None:
    closet(session, storage, user_a)
    set_home(session, user_a)
    weather.conditions = Conditions(22.0, 18.0)
    response = await post_form(client, "/outfits/generate", {"today": "1", "season": "spring"})
    assert response.text.count('class="card outfit"') >= 1
    assert 'name="season" value="spring"' in response.text


async def test_provider_down_falls_back_to_manual_season_with_a_notice(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    weather: FakeWeatherProvider,
) -> None:
    closet(session, storage, user_a)
    set_home(session, user_a)
    weather.fail = True
    page = (await client.get("/outfits")).text
    assert "Weather is unavailable" in page and "Dress me for today" not in page
    response = await post_form(client, "/outfits/generate", {"today": "1", "season": "summer"})
    assert response.status_code == 200
    assert "Weather is unavailable right now. Choose a season instead." in response.text
    assert response.text.count('class="card outfit"') >= 1  # the manual season still works


async def test_nothing_suits_the_weather_says_so(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    weather: FakeWeatherProvider,
) -> None:
    for row in [
        (Category.TOP, "t-shirt", "white"),
        (Category.BOTTOM, "jeans", "blue"),
        (Category.SHOES, "sneakers", "white"),
    ]:
        piece(session, storage, user_a, *row)
    set_home(session, user_a)
    weather.conditions = Conditions(-4.0, -9.0)
    response = await post_form(client, "/outfits/generate", {"today": "1"})
    assert "No outfits yet" in response.text and "layer" in response.text.lower()
