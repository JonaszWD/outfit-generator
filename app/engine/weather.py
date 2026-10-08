"""Turn weather into outfit constraints. Pure: no network, no database.

A day's representative temperature falls in one band; the band says which seasons are
acceptable and whether a layer on top is expected. Rain adds its own rules (no open shoes, a
jacket or coat preferred). All numbers live in `WeatherConfig`.
"""

from dataclasses import dataclass, field, replace

from app.engine.types import Context


@dataclass(frozen=True)
class TemperatureBand:
    name: str
    below_c: float | None  # exclusive upper bound; None for the last band
    seasons: frozenset[str]
    require_outerwear: bool
    note: str


def _default_bands() -> tuple[TemperatureBand, ...]:
    return (
        TemperatureBand("cold", 5.0, frozenset({"winter"}), True, "Cold: wear a warm layer"),
        TemperatureBand(
            "chilly",
            12.0,
            frozenset({"winter", "autumn", "spring"}),
            True,
            "Chilly: a layer on top is expected",
        ),
        TemperatureBand(
            "mild", 18.0, frozenset({"autumn", "spring"}), False, "Mild: a light layer is optional"
        ),
        TemperatureBand(
            "warm", 25.0, frozenset({"spring", "summer"}), False, "Warm: light clothes"
        ),
        TemperatureBand("hot", None, frozenset({"summer"}), False, "Hot: keep it light"),
    )


@dataclass(frozen=True)
class WeatherConfig:
    bands: tuple[TemperatureBand, ...] = field(default_factory=_default_bands)
    rain_probability_percent: float = 50.0  # at or above this chance of rain...
    rain_millimetres: float = 1.0  # ...or at least this much expected, it counts as rainy


@dataclass(frozen=True)
class Conditions:
    """One day's weather, in degrees Celsius."""

    temp_high_c: float
    temp_low_c: float
    rain_probability_percent: float = 0.0
    rain_millimetres: float = 0.0

    @property
    def representative_c(self) -> float:
        """The temperature the bands are applied to: the mean of the day's high and low."""
        return (self.temp_high_c + self.temp_low_c) / 2


def band_for(temp_c: float, config: WeatherConfig) -> TemperatureBand:
    for band in config.bands:
        if band.below_c is None or temp_c < band.below_c:
            return band
    return config.bands[-1]


def is_rainy(conditions: Conditions, config: WeatherConfig) -> bool:
    return (
        conditions.rain_probability_percent >= config.rain_probability_percent
        or conditions.rain_millimetres >= config.rain_millimetres
    )


def apply_weather(base: Context, conditions: Conditions, config: WeatherConfig) -> Context:
    """Any manual season stays; the weather adds its constraints."""
    band = band_for(conditions.representative_c, config)
    return replace(
        base,
        temperature_c=round(conditions.representative_c, 1),
        acceptable_seasons=band.seasons,
        require_outerwear=band.require_outerwear,
        rain=is_rainy(conditions, config),
    )
