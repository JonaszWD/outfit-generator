from itertools import combinations

from app.engine.config import EngineConfig
from app.engine.generator import (
    closet_stats,
    count_outfits,
    enumerate_outfits,
    explain_empty,
    generate_outfits,
)
from app.engine.rules import failed_rules
from app.engine.types import Context
from app.engine.vocab import Category
from tests.engine.fixtures import CLOSET, big_closet, pick

CONFIG = EngineConfig()
ALL = list(CLOSET.values())


def jaccard(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    return len(set(a) & set(b)) / len(set(a) | set(b))


def test_generate_returns_the_requested_number_best_first() -> None:
    outfits = generate_outfits(ALL, Context(), CONFIG, 3, rng_seed=1)
    assert len(outfits) == 3
    assert outfits[0].score == max(o.score for o in outfits)
    for o in outfits:
        views = [CLOSET_BY_ID[i] for i in o.item_ids]
        assert failed_rules(views, Context(), CONFIG) == []


def test_top_results_are_not_near_duplicates() -> None:
    items = big_closet(150)
    outfits = generate_outfits(items, Context(), CONFIG, 6, rng_seed=2)
    assert len(outfits) == 6
    for a, b in combinations(outfits, 2):
        assert jaccard(a.base_ids, b.base_ids) < CONFIG.near_duplicate_similarity
        assert jaccard(a.item_ids, b.item_ids) < CONFIG.near_duplicate_similarity


def test_diversity_beats_plain_top_n() -> None:
    """Without the penalty, the best outfits are minor variations of each other."""
    from dataclasses import replace

    items = big_closet(150)
    plain = replace(CONFIG, diversity_penalty=0.0, near_duplicate_similarity=1.01)
    diverse = generate_outfits(items, Context(), CONFIG, 5, rng_seed=2)
    naive = generate_outfits(items, Context(), plain, 5, rng_seed=2)

    def worst_overlap(outfits: list) -> float:  # type: ignore[type-arg]
        return max(jaccard(a.base_ids, b.base_ids) for a, b in combinations(outfits, 2))

    assert worst_overlap(diverse) < worst_overlap(naive)


def test_accessories_are_added_when_they_fit() -> None:
    outfits = generate_outfits(ALL, Context(), CONFIG, 8, rng_seed=1)
    assert any(len(o.item_ids) > len(o.base_ids) for o in outfits)
    for o in outfits:
        accessories = o.item_ids[len(o.base_ids) :]
        assert len(accessories) <= CONFIG.max_accessories
        assert all(CLOSET_BY_ID[i].category == Category.ACCESSORY for i in accessories)


CLOSET_BY_ID = {v.id: v for v in CLOSET.values()}


def test_count_with_examples() -> None:
    result = count_outfits(ALL, CONFIG)
    assert result.count == len(list(enumerate_outfits(ALL, CONFIG)))
    assert result.count > 50
    assert 0 < len(result.examples) <= 5
    assert all(o.score >= CONFIG.min_score - 1e-9 for o in result.examples)


def test_must_include_counts_only_outfits_with_that_item() -> None:
    sweater = CLOSET["grey_sweater"]
    result = count_outfits(ALL, CONFIG, must_include=sweater)
    assert 0 < result.count < count_outfits(ALL, CONFIG).count
    assert all(sweater.id in o.item_ids for o in result.examples)


def test_a_candidate_that_fits_nothing_unlocks_nothing() -> None:
    from dataclasses import replace

    stray = replace(CLOSET["white_tee"], id=999, seasons=frozenset({"winter"}))
    closet = pick("white_tee", "blue_jeans", "tan_sandals")  # sandals are summer-only
    assert count_outfits(closet, CONFIG, must_include=stray).count == 0


def test_a_new_piece_unlocks_new_outfits() -> None:
    closet = pick("white_tee", "blue_jeans", "white_sneakers", "black_jeans")
    before = count_outfits(closet, CONFIG).count
    candidate = CLOSET["oxford_shirt"]
    unlocked = count_outfits(closet, CONFIG, must_include=candidate)
    after = count_outfits([*closet, candidate], CONFIG).count
    assert unlocked.count == after - before == 2  # with each of the two jeans


def test_stats_most_and_least_versatile() -> None:
    stats = closet_stats(ALL, CONFIG)
    assert stats.total == count_outfits(ALL, CONFIG).count
    ranked = sorted(stats.per_item, key=lambda i: stats.per_item[i])
    assert stats.per_item[ranked[0]] <= stats.per_item[ranked[-1]]
    assert stats.per_item[CLOSET["white_sneakers"].id] > stats.per_item[CLOSET["tan_sandals"].id]
    assert set(stats.per_item) == {i.id for i in ALL}


def test_season_changes_results() -> None:
    summer = count_outfits(ALL, CONFIG, context=Context(season="summer")).count
    winter = count_outfits(ALL, CONFIG, context=Context(season="winter")).count
    everything = count_outfits(ALL, CONFIG).count
    assert 0 < summer < everything and 0 < winter < everything


def test_min_score_is_configurable() -> None:
    from dataclasses import replace

    strict = replace(CONFIG, min_score=0.95)
    assert count_outfits(ALL, strict).count < count_outfits(ALL, CONFIG).count


def test_explain_empty_names_what_is_missing() -> None:
    assert explain_empty([], CONFIG) == [
        "No shoes yet.",
        "No tops yet (or a dress).",
        "No bottoms yet (or a dress).",
    ]
    no_shoes = pick("white_tee", "blue_jeans")
    assert explain_empty(no_shoes, CONFIG) == ["No shoes yet."]
    no_bottoms = pick("white_tee", "white_sneakers")
    assert explain_empty(no_bottoms, CONFIG) == ["No bottoms yet (or a dress)."]
    # a dress replaces the top and bottom, so only shoes are missing
    assert explain_empty(pick("floral_dress"), CONFIG) == ["No shoes yet."]


def test_explain_empty_for_incompatible_pieces() -> None:
    mismatched = pick("thermal_top", "denim_shorts", "white_sneakers")  # winter top, summer bottom
    (message,) = explain_empty(mismatched, CONFIG)
    assert "go together" in message
    winter_only = pick("grey_sweater", "blue_jeans", "white_sneakers")
    assert explain_empty(winter_only, CONFIG, Context(season="summer")) == [
        "No tops for summer yet (or a dress)."
    ]
    only_winter = pick("grey_sweater", "camel_coat", "thermal_top")
    assert explain_empty(only_winter, CONFIG, Context(season="summer")) == [
        "Nothing in your closet is tagged for summer."
    ]
