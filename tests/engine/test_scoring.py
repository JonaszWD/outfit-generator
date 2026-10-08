import pytest

from app.engine import vocab
from app.engine.config import EngineConfig
from app.engine.scoring import (
    ScoringConfig,
    color_pair_value,
    color_value,
    combine,
    pick_reasons,
    score_outfit,
)
from app.engine.types import Context
from tests.engine.fixtures import pick

SC = ScoringConfig()


@pytest.mark.parametrize(
    ("a", "b", "expected", "why"),
    [
        ("blue", "light blue", SC.analogous_value, "same hue family"),
        ("red", "burgundy", SC.analogous_value, "neighbours on the wheel"),
        ("orange", "yellow", SC.analogous_value, "neighbours on the wheel"),
        ("blue", "orange", SC.complementary_value, "opposites"),
        ("red", "blue", SC.complementary_value, "close enough to opposite"),
        ("red", "green", SC.triadic_value, "120 degrees apart"),
        ("yellow", "green", SC.clash_value, "about 85 degrees apart"),
        ("red", "red", SC.analogous_value, "same color"),
    ],
)
def test_color_pair_harmony(a: str, b: str, expected: float, why: str) -> None:
    assert color_pair_value(a, b, SC) == expected, why
    assert color_pair_value(b, a, SC) == expected


def test_neutrals_go_with_everything() -> None:
    for neutral in sorted(SC.neutral_colors):
        assert neutral in vocab.COLOR_HEX
    assert color_value(frozenset(), SC) == 1.0
    assert color_value(frozenset({"red"}), SC) == 1.0  # one color + neutrals is always fine


def test_neutral_defaults_cover_the_spec_list() -> None:
    required = {"black", "white", "grey", "navy", "beige", "cream", "brown"}
    assert required <= SC.neutral_colors


def test_weights_are_normalized() -> None:
    values = {"color": 0.8, "pattern": 0.8, "material": 0.8, "season": 0.8}
    assert combine(values, SC) == pytest.approx(0.8)  # any weights give 0.8 for equal values
    custom = ScoringConfig(weights={**SC.weights, "color": 5.0})
    skewed = {**values, "color": 0.2}
    assert combine(skewed, custom) < combine(skewed, SC)


def test_a_clash_pulls_the_score_down_much_more_than_an_average_would() -> None:
    items = pick("yellow_tee", "green_chinos", "white_sneakers")
    score, breakdown = score_outfit(items, Context(), SC)
    arithmetic = sum(SC.weights[b.rule] * b.value for b in breakdown) / sum(
        SC.weights[b.rule] for b in breakdown
    )
    assert score < EngineConfig().min_score
    assert score < arithmetic - 0.1


def test_reasons_are_plain_english_and_two_to_four() -> None:
    items = pick("oxford_shirt", "beige_chinos", "brown_loafers")
    _, breakdown = score_outfit(items, Context(), SC)
    reasons = pick_reasons(breakdown)
    assert 2 <= len(reasons) <= 4
    assert all(r and r[0].isupper() and not r.endswith(".") for r in reasons)


def test_neutral_reason_names_the_colors() -> None:
    items = pick("white_tee", "black_jeans", "white_sneakers")
    _, breakdown = score_outfit(items, Context(), SC)
    assert breakdown[0].reason == "White and black are all neutrals"


def test_pattern_reason_mentions_the_statement_piece() -> None:
    items = pick("striped_tee", "beige_chinos", "white_sneakers")
    _, breakdown = score_outfit(items, Context(), SC)
    assert any("Striped" in r for r in pick_reasons(breakdown))
