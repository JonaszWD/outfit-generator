"""Property tests: the fast generator must agree with a slow, obviously-correct reference."""

import itertools
import random
from dataclasses import replace

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.engine import vocab
from app.engine.config import EngineConfig
from app.engine.generator import (
    closet_stats,
    count_outfits,
    enumerate_outfits,
    generate_outfits,
    meets_minimum,
)
from app.engine.rules import accessory_fits, failed_rules
from app.engine.scoring import score_outfit
from app.engine.types import ALL_SEASONS, Context, ItemView, Outfit
from app.engine.vocab import Category

CONFIG = EngineConfig()
SETTINGS = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

NEUTRAL_BIASED = [*vocab.COLOR_NAMES, "black", "white", "grey", "navy", "beige", "navy", "white"]
CATEGORY_BIASED = [
    Category.TOP,
    Category.TOP,
    Category.BOTTOM,
    Category.BOTTOM,
    Category.SHOES,
    Category.SHOES,
    Category.OUTERWEAR,
    Category.DRESS,
    Category.ACCESSORY,
    Category.ACCESSORY,
]


@st.composite
def item_views(draw: st.DrawFn, item_id: int, forced: Category | None = None) -> ItemView:
    category = forced or draw(st.sampled_from(CATEGORY_BIASED))
    return ItemView(
        id=item_id,
        category=category,
        subtype=draw(st.sampled_from(vocab.SUBTYPES[category])),
        primary_color=draw(st.sampled_from(NEUTRAL_BIASED)),
        secondary_colors=tuple(draw(st.lists(st.sampled_from(vocab.COLOR_NAMES), max_size=1))),
        pattern=draw(st.sampled_from(["solid"] * 5 + list(vocab.PATTERNS[1:]))),
        seasons=draw(
            st.one_of(
                st.just(ALL_SEASONS),
                st.lists(st.sampled_from(vocab.SEASONS), min_size=1, max_size=4, unique=True).map(
                    frozenset
                ),
            )
        ),
        material=draw(st.sampled_from([None, *vocab.MATERIALS])),
    )


@st.composite
def closets(draw: st.DrawFn, min_size: int = 0, max_size: int = 13) -> list[ItemView]:
    """Usually a closet that can make outfits: half the time it starts with a top, a bottom
    and shoes, so the properties are not vacuously true on empty results."""
    size = draw(st.integers(min_size, max_size))
    basics = [Category.TOP, Category.BOTTOM, Category.SHOES] if draw(st.booleans()) else []
    forced = [*basics, *([None] * max(0, size - len(basics)))]
    return [draw(item_views(k + 1, forced=f)) for k, f in enumerate(forced)]


contexts = st.builds(
    Context,
    season=st.sampled_from([None, *vocab.SEASONS]),
    acceptable_seasons=st.one_of(
        st.none(),
        st.lists(st.sampled_from(vocab.SEASONS), min_size=1, max_size=3, unique=True).map(
            frozenset
        ),
    ),
    require_outerwear=st.booleans(),
    rain=st.booleans(),
)

# The default config and a stricter one (a higher bar changes which outfits are valid).
STRICT_CONFIG = replace(CONFIG, min_score=0.75)
configs = st.sampled_from([CONFIG, STRICT_CONFIG])


def brute_force(
    items: list[ItemView], context: Context, config: EngineConfig
) -> set[tuple[int, ...]]:
    """Reference: try every structure with the slow rule functions and slow scoring."""
    by = {c: [i for i in items if i.category == c] for c in Category}
    cores: list[tuple[ItemView, ...]] = [
        (t, b) for t in by[Category.TOP] for b in by[Category.BOTTOM]
    ]
    cores += [(d,) for d in by[Category.DRESS]]
    found: set[tuple[int, ...]] = set()
    outers: list[ItemView | None] = [None, *by[Category.OUTERWEAR]]
    for core, shoes, outer in itertools.product(cores, by[Category.SHOES], outers):
        outfit = [*core, shoes, *([outer] if outer else [])]
        if failed_rules(outfit, context, config):
            continue
        score, _ = score_outfit(outfit, context, config.scoring, config.rain_outerwear)
        if meets_minimum(score, config):
            found.add(tuple(sorted(i.id for i in outfit)))
    return found


def keys(outfits: list[Outfit]) -> set[tuple[int, ...]]:
    return {o.key for o in outfits}


@settings(parent=SETTINGS, max_examples=300)
@given(closets(), contexts, configs)
def test_enumeration_matches_brute_force(
    items: list[ItemView], context: Context, config: EngineConfig
) -> None:
    fast = list(enumerate_outfits(items, config, context))
    assert len(fast) == len({o.key for o in fast}), "duplicate outfits"
    assert keys(fast) == brute_force(items, context, config)
    assert count_outfits(items, config, context=context).count == len(fast)


@SETTINGS
@given(closets(min_size=3), contexts, st.data())
def test_must_include_equals_outfits_containing_item(
    items: list[ItemView], context: Context, data: st.DataObject
) -> None:
    item = data.draw(st.sampled_from(items))
    result = count_outfits(items, CONFIG, must_include=item, context=context)
    everything = list(enumerate_outfits(items, CONFIG, context))
    if item.category == Category.ACCESSORY:
        by_id = {i.id: i for i in items}
        expected = sum(
            accessory_fits([by_id[i] for i in o.base_ids], item, context, CONFIG)
            for o in everything
        )
    else:
        expected = sum(item.id in o.item_ids for o in everything)
    assert result.count == expected
    for example in result.examples:
        assert item.id in example.item_ids


@SETTINGS
@given(closets(min_size=2), contexts, st.data())
def test_must_include_item_outside_the_closet(
    items: list[ItemView], context: Context, data: st.DataObject
) -> None:
    """Phase 4 passes a product the user does not own yet."""
    candidate = data.draw(item_views(len(items) + 1))
    with_it = count_outfits([*items, candidate], CONFIG, must_include=candidate, context=context)
    without_it = count_outfits(items, CONFIG, must_include=candidate, context=context)
    assert with_it.count == without_it.count


@SETTINGS
@given(closets(), contexts, st.integers(0, 3), st.integers(1, 6))
def test_generated_outfits_always_satisfy_the_rules(
    items: list[ItemView], context: Context, seed: int, limit: int
) -> None:
    by_id = {i.id: i for i in items}
    outfits = generate_outfits(items, context, CONFIG, limit, rng_seed=seed)
    assert len(outfits) <= limit
    for outfit in outfits:
        views = [by_id[i] for i in outfit.item_ids]
        assert failed_rules(views, context, CONFIG) == []
        assert 0.0 <= outfit.score <= 1.0 and meets_minimum(outfit.score, CONFIG)
        assert all(0.0 <= b.value <= 1.0 for b in outfit.breakdown)
        assert 2 <= len(outfit.reasons) <= 4
        assert len(set(outfit.item_ids)) == len(outfit.item_ids)
        assert outfit.item_ids[: len(outfit.base_ids)] == outfit.base_ids


@SETTINGS
@given(closets(), contexts, st.integers(0, 10_000))
def test_same_seed_gives_same_output(items: list[ItemView], context: Context, seed: int) -> None:
    first = generate_outfits(items, context, CONFIG, 4, rng_seed=seed)
    second = generate_outfits(items, context, CONFIG, 4, rng_seed=seed)
    assert first == second


@SETTINGS
@given(closets(), item_views(100), contexts)
def test_adding_an_item_never_reduces_the_count(
    items: list[ItemView], extra: ItemView, context: Context
) -> None:
    before = count_outfits(items, CONFIG, context=context).count
    after = count_outfits([*items, extra], CONFIG, context=context).count
    assert after >= before


@SETTINGS
@given(closets(), contexts)
def test_stats_agree_with_enumeration(items: list[ItemView], context: Context) -> None:
    stats = closet_stats(items, CONFIG, context)
    everything = list(enumerate_outfits(items, CONFIG, context))
    assert stats.total == len(everything)
    by_id = {i.id: i for i in items}
    for item in items:
        if item.category == Category.ACCESSORY:
            expected = sum(
                accessory_fits([by_id[i] for i in o.base_ids], item, context, CONFIG)
                for o in everything
            )
        else:
            expected = sum(item.id in o.item_ids for o in everything)
        assert stats.per_item[item.id] == expected, item


@SETTINGS
@given(closets(min_size=4), contexts, st.integers(0, 3))
def test_exclude_removes_exactly_those_outfits(
    items: list[ItemView], context: Context, seed: int
) -> None:
    first = generate_outfits(items, context, CONFIG, 3, rng_seed=seed)
    banned = {o.base_key for o in first}
    again = generate_outfits(items, context, CONFIG, 3, rng_seed=seed, exclude=banned)
    assert not ({o.base_key for o in again} & banned)


def test_random_tie_breaking_varies_without_a_seed() -> None:
    # Ten identical-looking all-neutral tops make every top equally good.
    from tests.engine.fixtures import neutral_closet

    items = neutral_closet(60)
    picks = {generate_outfits(items, Context(), CONFIG, 1)[0].base_key for _ in range(12)}
    assert len(picks) > 1
    seeded = {
        generate_outfits(items, Context(), CONFIG, 1, rng_seed=3)[0].base_key for _ in range(5)
    }
    assert len(seeded) == 1
    assert ALL_SEASONS  # keep the import used
    assert random  # keep the import used
