import random
from dataclasses import replace

import pytest

from app.engine.config import EngineConfig
from app.engine.generator import count_outfits, generate_outfits
from app.engine.personalize import (
    NONE,
    FeedbackSample,
    Personalization,
    PersonalizationConfig,
    adjusted_score,
    apply_preferences,
    derive,
)
from app.engine.scoring import score_outfit
from app.engine.types import Context, ItemView, Outfit
from tests.engine.fixtures import CLOSET, pick

CONFIG = EngineConfig()
ALL = list(CLOSET.values())
BY_ID = {i.id: i for i in ALL}
PCFG = PersonalizationConfig()


def sample(outfit: Outfit, rating: int, reason: str | None = None) -> FeedbackSample:
    return FeedbackSample(
        tuple(sorted(outfit.item_ids)),
        rating,
        reason,
        {b.rule: b.value for b in outfit.breakdown},
    )


def baseline(limit: int = 10) -> list[Outfit]:
    return generate_outfits(ALL, Context(), CONFIG, limit, rng_seed=1)


def views(outfit: Outfit) -> list[ItemView]:
    return [BY_ID[i] for i in outfit.item_ids]


def personalized(personalization: Personalization, limit: int = 10) -> list[Outfit]:
    return generate_outfits(
        ALL, Context(), CONFIG, limit, rng_seed=1, personalization=personalization
    )


def ranked(outfits: list[Outfit]) -> list[tuple[int, ...]]:
    return [o.key for o in outfits]


# -------------------------------------------------------------------- derivation


def test_no_feedback_is_the_default_exactly() -> None:
    result = derive(CONFIG.scoring, [])
    assert result.scoring == CONFIG.scoring and not result.active and result.samples == 0
    assert NONE.active is False


def test_a_color_complaint_raises_the_color_weight() -> None:
    outfit = baseline()[0]
    result = derive(CONFIG.scoring, [sample(outfit, -1, "colors clash")])
    assert result.weight_ratios["color"] > 1.0
    assert result.scoring.weights["color"] > CONFIG.scoring.weights["color"]
    assert result.weight_ratios["pattern"] == pytest.approx(1.0)


def test_rules_where_disliked_outfits_scored_poorly_gain_weight() -> None:
    poor = FeedbackSample((1, 2, 3), -1, None, {"color": 0.2, "pattern": 1.0, "season": 1.0})
    result = derive(CONFIG.scoring, [poor] * 4)
    assert result.weight_ratios["color"] > 1.0
    assert result.weight_ratios["pattern"] == pytest.approx(1.0)


def test_liked_outfits_that_scored_poorly_on_a_rule_lower_its_weight() -> None:
    liked = FeedbackSample((1, 2, 3), 1, None, {"pattern": 0.3, "color": 1.0})
    result = derive(CONFIG.scoring, [liked] * 4)
    assert result.weight_ratios["pattern"] < 1.0


def test_few_ratings_move_things_only_a_little() -> None:
    one = derive(CONFIG.scoring, [FeedbackSample((1, 2, 3), -1, "colors clash", {})])
    complaints = [FeedbackSample((i, 2, 3), -1, "colors clash", {}) for i in range(40)]
    many = derive(CONFIG.scoring, complaints)
    assert 1.0 < one.weight_ratios["color"] < many.weight_ratios["color"]


def test_caps_hold_no_matter_how_much_feedback() -> None:
    rng = random.Random(0)
    samples = []
    for _ in range(3000):
        ids = tuple(sorted(rng.sample(range(1, 40), 4)))
        samples.append(
            FeedbackSample(
                ids,
                rng.choice([-1, 1]),
                rng.choice([None, "colors clash", "wrong for weather"]),
                {r: rng.random() for r in ("color", "pattern", "material", "season")},
            )
        )
    result = derive(CONFIG.scoring, samples)
    for rule, ratio in result.weight_ratios.items():
        assert 1 - PCFG.max_weight_change - 1e-9 <= ratio <= 1 + PCFG.max_weight_change + 1e-9, rule
    for value in result.pair_adjustments.values():
        assert PCFG.pair_min - 1e-9 <= value <= PCFG.pair_max + 1e-9
    # the total change to one outfit's score is capped too
    outfit = baseline()[0]
    score, _ = score_outfit([BY_ID[i] for i in outfit.item_ids], Context(), CONFIG.scoring)
    packed = replace(
        result,
        pair_adjustments=dict.fromkeys(
            [(a, b) for a in outfit.item_ids for b in outfit.item_ids if a < b], -1.0
        ),
    )
    tuned, _ = adjusted_score([BY_ID[i] for i in outfit.item_ids], Context(), packed, score)
    assert tuned >= 0.0 and score - tuned <= 1.0


def test_derivation_is_deterministic_and_order_independent() -> None:
    outfits = baseline(6)
    samples = [sample(o, r) for o, r in zip(outfits, (-1, 1, -1, 1, -1, 1), strict=True)]
    first = derive(CONFIG.scoring, samples)
    shuffled = list(reversed(samples))
    assert derive(CONFIG.scoring, shuffled) == first
    assert derive(CONFIG.scoring, samples) == first


# ----------------------------------------------------------------------- ranking


def test_disliking_an_outfit_pushes_it_down() -> None:
    before = baseline(10)
    victim = before[0]
    personalization = derive(CONFIG.scoring, [sample(victim, -1)])
    after = personalized(personalization)
    again = next((o for o in after if o.key == victim.key), None)
    if again is not None:
        assert again.adjusted_score is not None and again.adjusted_score < again.score
        assert ranked(after).index(victim.key) > 0
        assert "Contains a pairing you disliked" in again.adjustment_notes
    assert after[0].key != victim.key


def test_liking_an_outfit_pulls_it_up() -> None:
    # red + blue is complementary (score below 1), so a like has room to raise it
    closet = pick("red_tee", "white_tee", "blue_jeans", "black_jeans", "white_sneakers")
    before = generate_outfits(closet, Context(), CONFIG, 10, rng_seed=1)
    favorite = next(o for o in before if o.score < 0.99)
    personalization = derive(CONFIG.scoring, [sample(favorite, 1)])
    after = generate_outfits(
        closet, Context(), CONFIG, 10, rng_seed=1, personalization=personalization
    )
    now = next(o for o in after if o.key == favorite.key)
    assert now.adjusted_score is not None and now.adjusted_score > now.score
    assert ranked(after).index(favorite.key) <= ranked(before).index(favorite.key)
    assert "Contains a pairing you liked" in now.adjustment_notes


def test_the_base_score_is_never_hidden_or_changed() -> None:
    before = {o.key: o.score for o in baseline(10)}
    victim = baseline(10)[0]
    personalization = derive(CONFIG.scoring, [sample(victim, -1, "colors clash")] * 3)
    after = personalized(personalization)
    for outfit in after:
        if outfit.key in before:
            assert outfit.score == pytest.approx(before[outfit.key])
        assert outfit.score >= CONFIG.min_score - 1e-9  # validity is still the base score
        assert outfit.rank_score == (outfit.adjusted_score or outfit.score)


def test_the_counts_ignore_personalization() -> None:
    assert count_outfits(ALL, CONFIG).count == count_outfits(ALL, CONFIG).count
    # personalization is an argument of generate_outfits only: counts have no such parameter


def test_resetting_restores_the_default_output_exactly() -> None:
    plain = baseline(8)
    victim = plain[0]
    tuned = derive(CONFIG.scoring, [sample(victim, -1, "colors clash"), sample(plain[3], 1)])
    changed = generate_outfits(ALL, Context(), CONFIG, 8, rng_seed=1, personalization=tuned)
    assert changed != plain
    reset = derive(CONFIG.scoring, [])  # all feedback deleted
    restored = generate_outfits(ALL, Context(), CONFIG, 8, rng_seed=1, personalization=reset)
    assert restored == plain


def test_a_weather_complaint_does_not_penalize_the_pairing() -> None:
    outfit = baseline()[0]
    result = derive(CONFIG.scoring, [sample(outfit, -1, "wrong for weather")])
    assert result.pair_adjustments == {}
    assert result.weight_ratios["season"] > 1.0


# ------------------------------------------------------------------- preferences


def test_default_preferences_reproduce_the_default_config_exactly() -> None:
    assert apply_preferences(CONFIG, 0.5) == CONFIG


def test_color_strictness_changes_what_counts_as_valid() -> None:
    loose = apply_preferences(CONFIG, 0.0)
    strict = apply_preferences(CONFIG, 1.0)
    assert loose.max_non_neutral_colors == 3 and strict.max_non_neutral_colors == 1
    assert strict.scoring.clash_value < CONFIG.scoring.clash_value < loose.scoring.clash_value
    counts = [count_outfits(ALL, c).count for c in (strict, CONFIG, loose)]
    assert counts[0] <= counts[1] <= counts[2] and counts[0] < counts[2]
