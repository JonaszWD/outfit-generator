"""docs/outfit-engine.md lists every default; keep it in sync with the code."""

import re
from dataclasses import fields
from pathlib import Path

import pytest

from app.engine.config import EngineConfig
from app.engine.sanity import SANITY_RULES
from app.engine.scoring import ScoringConfig

DOCS = (Path(__file__).resolve().parents[2] / "docs" / "outfit-engine.md").read_text()
CONFIG = EngineConfig()


def scalar_fields() -> list[tuple[str, object]]:
    rows: list[tuple[str, object]] = []
    for owner in (CONFIG, CONFIG.scoring):
        for f in fields(owner):
            value = getattr(owner, f.name)
            if isinstance(value, int | float) and not isinstance(value, bool):
                rows.append((f.name, value))
    return rows


@pytest.mark.parametrize(("name", "value"), scalar_fields(), ids=lambda v: str(v))
def test_documented_default_matches_code(name: str, value: object) -> None:
    rows = [line for line in DOCS.splitlines() if line.startswith(f"| `{name}` |")]
    assert rows, f"{name} is missing from docs/outfit-engine.md"
    defaults = {cells[2].strip() for cells in (r.split("|") for r in rows)}
    assert any(d == str(value) or (d.replace("`", "") == str(value)) for d in defaults), (
        f"{name}: docs say {defaults}, code says {value}"
    )


def test_every_weight_is_documented() -> None:
    for rule, weight in CONFIG.scoring.weights.items():
        assert re.search(rf"`{rule}`\s*\|\s*{weight}\b", DOCS) or f"`{rule}` {weight}" in DOCS, rule


def test_every_sanity_rule_is_documented() -> None:
    for rule in SANITY_RULES:
        assert rule.reason in DOCS, rule.reason


def test_neutral_colors_are_documented() -> None:
    for color in ScoringConfig().neutral_colors:
        assert color in DOCS


def test_weather_bands_are_documented() -> None:
    previous = None
    for band in CONFIG.weather.bands:
        seasons = ", ".join(
            s for s in ("winter", "autumn", "spring", "summer") if s in band.seasons
        )
        layer = "required" if band.require_outerwear else "optional"
        if band.below_c is None:
            temperature = f"{previous:g} C and above"
        else:
            temperature = (
                f"below {band.below_c:g} C"
                if previous is None
                else f"{previous:g} to below {band.below_c:g} C"
            )
        row = f"| {band.name} | {temperature} | {seasons} | {layer} |"
        assert row in DOCS, row
        previous = band.below_c


def test_weather_thresholds_are_documented() -> None:
    weather = CONFIG.weather
    assert f"{weather.rain_probability_percent:g} percent" in DOCS
    assert f"{weather.rain_millimetres:g} mm" in DOCS
