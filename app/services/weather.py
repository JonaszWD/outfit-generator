"""Forecasts and place lookup behind a small interface.

`OpenMeteoProvider` needs no API key. The only data it sends out is what the user typed as
their city (geocoding) and the home coordinates, rounded to 2 decimals (about 1 km) before they
leave this server. Failures never break a page: callers get a `Forecast` with `days=[]` and a
notice, and the UI falls back to choosing a season by hand.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.engine.weather import Conditions

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 7
UNAVAILABLE = "Weather is unavailable right now. Choose a season instead."


class WeatherError(Exception):
    """The provider could not answer (network, timeout, bad response)."""


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class DayForecast:
    day: date
    conditions: Conditions


@dataclass(frozen=True)
class Forecast:
    days: list[DayForecast]
    notice: str | None = None  # set when the forecast could not be loaded

    def for_day(self, day: date) -> Conditions | None:
        return next((d.conditions for d in self.days if d.day == day), None)

    @property
    def today(self) -> DayForecast | None:
        return self.days[0] if self.days else None


class WeatherProvider(Protocol):
    def geocode(self, query: str) -> list[Place]: ...

    def forecast(self, latitude: float, longitude: float) -> list[DayForecast]:
        """Up to seven days starting today, in degrees Celsius. Raises WeatherError."""
        ...


def round_coordinate(value: float) -> float:
    return round(value, 2)


class OpenMeteoProvider:
    def __init__(self, timeout: float = 5.0, client: httpx.Client | None = None) -> None:
        self.timeout = timeout
        self._client = client

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if self._client is not None:
                response = self._client.get(url, params=params, timeout=self.timeout)
            else:
                response = httpx.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WeatherError(type(exc).__name__) from exc
        if not isinstance(data, dict):
            raise WeatherError("unexpected response")
        return data

    def geocode(self, query: str) -> list[Place]:
        query = query.strip()
        if not query:
            return []
        data = self._get(GEOCODE_URL, {"name": query[:100], "count": 5, "language": "en"})
        places = []
        for row in data.get("results") or []:
            try:
                label = ", ".join(str(row[k]) for k in ("name", "admin1", "country") if row.get(k))
                places.append(
                    Place(
                        label,
                        round_coordinate(float(row["latitude"])),
                        round_coordinate(float(row["longitude"])),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return places

    def forecast(self, latitude: float, longitude: float) -> list[DayForecast]:
        data = self._get(
            FORECAST_URL,
            {
                "latitude": round_coordinate(latitude),
                "longitude": round_coordinate(longitude),
                "daily": "temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,precipitation_sum",
                "forecast_days": FORECAST_DAYS,
                "timezone": "auto",
            },
        )
        try:
            daily = data["daily"]
            days = []
            for i, raw_day in enumerate(daily["time"]):
                days.append(
                    DayForecast(
                        date.fromisoformat(raw_day),
                        Conditions(
                            temp_high_c=float(daily["temperature_2m_max"][i]),
                            temp_low_c=float(daily["temperature_2m_min"][i]),
                            rain_probability_percent=float(
                                daily["precipitation_probability_max"][i] or 0
                            ),
                            rain_millimetres=float(daily["precipitation_sum"][i] or 0),
                        ),
                    )
                )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise WeatherError("unexpected response") from exc
        if not days:
            raise WeatherError("empty forecast")
        return days


@dataclass
class FakeWeatherProvider:
    """For tests and offline development: fixed places, a settable forecast, optional failure."""

    conditions: Conditions = field(default_factory=lambda: Conditions(18.0, 10.0))
    places: list[Place] = field(default_factory=lambda: [Place("Oslo, Norway", 59.91, 10.75)])
    fail: bool = False
    calls: int = 0
    requested: list[tuple[float, float]] = field(default_factory=list)
    start: date | None = None

    def geocode(self, query: str) -> list[Place]:
        if self.fail:
            raise WeatherError("down")
        return [p for p in self.places if query.strip().lower() in p.name.lower()]

    def forecast(self, latitude: float, longitude: float) -> list[DayForecast]:
        self.calls += 1
        self.requested.append((latitude, longitude))
        if self.fail:
            raise WeatherError("down")
        first = self.start or date.today()
        return [DayForecast(first + timedelta(days=i), self.conditions) for i in range(7)]


class CachedWeather:
    """Wraps a provider: forecasts are kept for `ttl` seconds per rounded location, in memory.

    The cache is per process, which is fine for one app server; with several workers each keeps
    its own. Failures are cached for a short time so a dead API is not hit on every page view.
    """

    FAILURE_TTL = 60.0

    def __init__(
        self,
        provider: WeatherProvider,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.ttl = ttl_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple[float, float], tuple[float, list[DayForecast] | None]] = {}

    def geocode(self, query: str) -> list[Place]:
        return self.provider.geocode(query)

    def forecast(self, latitude: float, longitude: float) -> list[DayForecast]:
        key = (round_coordinate(latitude), round_coordinate(longitude))
        now = self.clock()
        with self._lock:
            hit = self._entries.get(key)
        if hit is not None and hit[0] > now:
            if hit[1] is None:
                raise WeatherError("recently failed")
            return hit[1]
        try:
            days = self.provider.forecast(*key)
        except WeatherError:
            with self._lock:
                self._entries[key] = (now + self.FAILURE_TTL, None)
            raise
        with self._lock:
            self._entries[key] = (now + self.ttl, days)
        return days


class NoWeather:
    """WEATHER_PROVIDER=none: no outside calls at all."""

    def geocode(self, query: str) -> list[Place]:
        raise WeatherError("disabled")

    def forecast(self, latitude: float, longitude: float) -> list[DayForecast]:
        raise WeatherError("disabled")


def build_weather_provider(settings: Settings) -> WeatherProvider:
    if settings.weather_provider == "none":
        return NoWeather()
    return CachedWeather(
        OpenMeteoProvider(settings.weather_timeout_seconds),
        settings.weather_cache_minutes * 60.0,
    )


def load_forecast(
    provider: WeatherProvider, latitude: float | None, longitude: float | None
) -> Forecast:
    """The forecast for a home location, or an empty one with a notice saying why."""
    if latitude is None or longitude is None:
        return Forecast([], "Set your home city in preferences to see the weather.")
    try:
        return Forecast(provider.forecast(latitude, longitude))
    except WeatherError:
        logger.warning("weather unavailable")
        return Forecast([], UNAVAILABLE)


def to_fahrenheit(celsius: float) -> float:
    return celsius * 9 / 5 + 32


def format_temperature(celsius: float, units: str) -> str:
    value = to_fahrenheit(celsius) if units == "imperial" else celsius
    return f"{round(value)}°{'F' if units == 'imperial' else 'C'}"
