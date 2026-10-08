"""Per-user preferences: home city, color strictness and units."""

from dataclasses import dataclass

from sqlmodel import Session

from app.engine.config import EngineConfig
from app.engine.personalize import apply_preferences
from app.models import User, UserPreferences
from app.services.weather import Place, WeatherError, WeatherProvider, round_coordinate

UNITS = ("metric", "imperial")


@dataclass(frozen=True)
class PreferenceInput:
    color_strictness: float
    units: str


def get_preferences(session: Session, user: User) -> UserPreferences:
    """The user's row, or an unsaved default (the engine defaults) when none exists."""
    prefs = session.get(UserPreferences, user.id)
    return prefs if prefs is not None else UserPreferences(user_id=user.id or 0)


def parse_preferences(strictness: str, units: str) -> tuple[PreferenceInput | None, dict[str, str]]:
    errors: dict[str, str] = {}
    try:
        strict = float(strictness)
        if not 0.0 <= strict <= 1.0:
            raise ValueError
    except ValueError:
        errors["color_strictness"] = "Choose a value from loose to strict."
        strict = 0.5
    if units not in UNITS:
        errors["units"] = "Choose metric or imperial."
    if errors:
        return None, errors
    return PreferenceInput(round(strict, 2), units), {}


def save_preferences(session: Session, user: User, data: PreferenceInput) -> UserPreferences:
    prefs = session.get(UserPreferences, user.id)
    if prefs is None:
        assert user.id is not None
        prefs = UserPreferences(user_id=user.id)
    prefs.color_strictness = data.color_strictness
    prefs.units = data.units
    session.add(prefs)
    session.commit()
    session.refresh(prefs)
    return prefs


def search_places(provider: WeatherProvider, query: str) -> tuple[list[Place], bool]:
    """(matches, provider_ok)."""
    try:
        return provider.geocode(query), True
    except WeatherError:
        return [], False


def set_home(session: Session, user: User, place: Place | None) -> UserPreferences:
    """Store the home city. Coordinates are rounded to 2 decimals before they are stored."""
    prefs = session.get(UserPreferences, user.id)
    if prefs is None:
        assert user.id is not None
        prefs = UserPreferences(user_id=user.id)
    if place is None:
        prefs.home_city = prefs.latitude = prefs.longitude = None
    else:
        prefs.home_city = place.name[:120]
        prefs.latitude = round_coordinate(place.latitude)
        prefs.longitude = round_coordinate(place.longitude)
    session.add(prefs)
    session.commit()
    session.refresh(prefs)
    return prefs


def engine_config_for(
    session: Session, user: User, base: EngineConfig | None = None
) -> EngineConfig:
    """The engine configuration with this user's explicit preferences applied."""
    prefs = session.get(UserPreferences, user.id)
    base = base or EngineConfig()
    if prefs is None:
        return base
    return apply_preferences(base, prefs.color_strictness)
