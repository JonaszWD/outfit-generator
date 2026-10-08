import pytest

from app.engine.config import EngineConfig
from app.engine.generator import count_outfits, explain_empty, generate_outfits
from app.engine.rules import failed_rules
from app.engine.scoring import score_outfit
from app.engine.types import Context
from app.engine.vocab import Category
from app.engine.weather import (
    Conditions,
    TemperatureBand,
    WeatherConfig,
    apply_weather,
    band_for,
    is_rainy,
)
from tests.engine.fixtures import CLOSET, pick

CONFIG = EngineConfig()
WEATHER = WeatherConfig()
ALL = list(CLOSET.values())
BY_ID = {i.id: i for i in ALL}


def day(high: float, low: float | None = None, **rain: float) -> Conditions:
    return Conditions(high, high if low is None else low, **rain)


@pytest.mark.parametrize(
    ("temp", "band"),
    [
        (-8, "cold"),
        (4.9, "cold"),
        (5, "chilly"),
        (11.9, "chilly"),
        (12, "mild"),
        (17.9, "mild"),
        (18, "warm"),
        (24.9, "warm"),
        (25, "hot"),
        (38, "hot"),
    ],
)
def test_temperature_bands(temp: float, band: str) -> None:
    assert band_for(temp, WEATHER).name == band


def test_below_12_degrees_expects_a_layer_and_warm_days_do_not() -> None:
    for temp in (-5, 4, 8, 11.9):
        assert band_for(temp, WEATHER).require_outerwear
    for temp in (12, 20, 30):
        assert not band_for(temp, WEATHER).require_outerwear


def test_representative_temperature_is_the_mean_of_high_and_low() -> None:
    assert day(14, 6).representative_c == 10
    assert band_for(day(14, 6).representative_c, WEATHER).name == "chilly"


@pytest.mark.parametrize(
    ("probability", "mm", "rainy"),
    [(0, 0, False), (49, 0.9, False), (50, 0, True), (10, 1.0, True), (100, 5, True)],
)
def test_rain_detection(probability: float, mm: float, rainy: bool) -> None:
    conditions = day(15, rain_probability_percent=probability, rain_millimetres=mm)
    assert is_rainy(conditions, WEATHER) is rainy


def test_apply_weather_keeps_the_season_and_adds_constraints() -> None:
    base = Context(season="winter")
    context = apply_weather(base, day(10, 4, rain_probability_percent=80), WEATHER)
    assert context.season == "winter"
    assert context.acceptable_seasons == frozenset({"winter", "autumn", "spring"})
    assert context.require_outerwear and context.rain and context.temperature_c == 7.0


def test_bands_are_configurable() -> None:
    cfg = WeatherConfig(
        bands=(TemperatureBand("any", None, frozenset({"summer"}), False, "Always summer"),)
    )
    context = apply_weather(Context(), day(-20), cfg)
    assert context.acceptable_seasons == frozenset({"summer"}) and not context.require_outerwear


def test_cold_weather_requires_a_layer_and_winter_pieces() -> None:
    context = apply_weather(Context(), day(2, 0), WEATHER)
    outfits = generate_outfits(ALL, context, CONFIG, 8, rng_seed=1)
    assert outfits
    for outfit in outfits:
        pieces = [BY_ID[i] for i in outfit.item_ids]
        assert any(p.category == Category.OUTERWEAR for p in pieces)
        assert all("winter" in p.seasons for p in pieces)
        assert failed_rules(pieces, context, CONFIG) == []


def test_hot_weather_excludes_winter_only_pieces() -> None:
    context = apply_weather(Context(), day(31, 22), WEATHER)
    for outfit in generate_outfits(ALL, context, CONFIG, 8, rng_seed=1):
        assert all("summer" in BY_ID[i].seasons for i in outfit.item_ids)


def test_weather_narrows_the_count() -> None:
    everything = count_outfits(ALL, CONFIG).count
    cold = count_outfits(ALL, CONFIG, context=apply_weather(Context(), day(1), WEATHER)).count
    hot = count_outfits(ALL, CONFIG, context=apply_weather(Context(), day(30), WEATHER)).count
    assert 0 < cold < everything and 0 < hot < everything


def test_rain_rules_out_open_shoes() -> None:
    context = apply_weather(Context(), day(20, rain_probability_percent=90), WEATHER)
    assert context.rain
    for outfit in generate_outfits(ALL, context, CONFIG, 10, rng_seed=1):
        assert not any(BY_ID[i].subtype == "sandals" for i in outfit.item_ids)
    assert (
        failed_rules(pick("linen_shirt", "beige_chinos", "tan_sandals"), context, CONFIG)[0].rule
        == "weather"
    )


def test_rain_prefers_a_jacket_or_coat() -> None:
    context = Context(rain=True)
    base = ["white_tee", "blue_jeans", "white_sneakers"]
    values = {}
    layers = (("none", None), ("jacket", "denim_jacket"), ("cardigan", "burgundy_cardigan"))
    for label, layer in layers:
        names = base if layer is None else [*base, layer]
        _, breakdown = score_outfit(pick(*names), context, CONFIG.scoring)
        values[label] = next(b for b in breakdown if b.rule == "weather")
    assert values["jacket"].value == 1.0 > values["cardigan"].value > values["none"].value
    assert "keeps you dry" in values["jacket"].reason
    _, dry = score_outfit(pick(*base), Context(), CONFIG.scoring)
    assert "weather" not in {b.rule for b in dry}


def test_rain_lifts_outfits_with_a_jacket_in_the_ranking() -> None:
    dry = generate_outfits(ALL, Context(), CONFIG, 8, rng_seed=1)
    wet = generate_outfits(ALL, Context(rain=True), CONFIG, 8, rng_seed=1)

    def layered(outfits: list) -> int:  # type: ignore[type-arg]
        return sum(
            any(BY_ID[i].category == Category.OUTERWEAR for i in o.base_ids) for o in outfits
        )

    assert layered(wet) >= layered(dry)


def test_explain_empty_for_weather() -> None:
    cold = apply_weather(Context(), day(2), WEATHER)
    no_layers = pick("grey_sweater", "blue_jeans", "white_sneakers")
    assert explain_empty(no_layers, CONFIG, cold) == [
        "It is cold today and you have no jacket or coat for it yet."
    ]
    summer = pick("denim_shorts", "linen_shirt", "tan_sandals")
    assert explain_empty(summer, CONFIG, cold) == ["Nothing in your closet suits today's weather."]
