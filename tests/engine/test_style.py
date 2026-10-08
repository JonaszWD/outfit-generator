"""Style references re-rank suggestions within a cap and never change what is valid."""

from app.engine.config import EngineConfig
from app.engine.generator import generate_outfits
from app.engine.rules import failed_rules
from app.engine.style import StyleConfig, StyleSample, build_profile, style_adjustment
from app.engine.types import Context
from tests.engine.fixtures import CLOSET, big_closet

CONFIG = EngineConfig()
ALL = list(CLOSET.values())
BY_ID = {i.id: i for i in ALL}

SMART_NAVY = StyleSample(("navy", "white"), ("solid",), ("shirt", "chinos", "loafers"))
SPORTY_GREEN = StyleSample(("olive", "green"), ("solid",), ("hoodie", "sneakers", "shorts"))


def test_no_profile_changes_nothing() -> None:
    plain = generate_outfits(ALL, Context(), CONFIG, 6, rng_seed=1)
    explicit = generate_outfits(ALL, Context(), CONFIG, 6, rng_seed=1, style=None)
    assert plain == explicit
    assert all(o.adjusted_by == () for o in plain)


def test_build_profile_needs_samples() -> None:
    assert build_profile([]) is None
    profile = build_profile([SMART_NAVY, SPORTY_GREEN])
    assert profile is not None and profile.samples == 2
    assert profile.colors["navy"] == 0.5


def test_style_outfits_stay_valid_and_keep_their_base_score() -> None:
    profile = build_profile([SMART_NAVY])
    base = {o.key: o.score for o in generate_outfits(ALL, Context(), CONFIG, 40, rng_seed=1)}
    styled = generate_outfits(ALL, Context(), CONFIG, 40, rng_seed=1, style=profile)
    assert styled
    for o in styled:
        assert failed_rules([BY_ID[i] for i in o.item_ids], Context(), CONFIG) == []
        assert o.score >= CONFIG.min_score - 1e-9
        if o.key in base:
            assert o.score == base[o.key]  # the base score is never rewritten


def test_different_references_surface_different_outfits() -> None:
    items = big_closet(150)
    smart = generate_outfits(
        items, Context(), CONFIG, 3, rng_seed=1, style=build_profile([SMART_NAVY])
    )
    sporty = generate_outfits(
        items, Context(), CONFIG, 3, rng_seed=1, style=build_profile([SPORTY_GREEN])
    )
    assert {o.key for o in smart} != {o.key for o in sporty}
    explained = [o for o in smart if "style" in o.adjusted_by]
    assert explained and all(o.adjustment_notes for o in explained)


def test_adjustment_is_capped_and_explained() -> None:
    profile = build_profile([SMART_NAVY])
    assert profile is not None
    cap = StyleConfig().max_adjustment
    for outfit in generate_outfits(ALL, Context(), CONFIG, 40, rng_seed=2):
        delta, notes = style_adjustment([BY_ID[i] for i in outfit.item_ids], profile)
        assert -cap <= delta <= cap
        assert bool(notes) == (delta != 0.0)
