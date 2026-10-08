"""Golden cases for the candidate analysis, on the hand-written fixture closet."""

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from app.engine.config import EngineConfig
from app.engine.generator import count_outfits
from app.engine.types import ItemView
from app.engine.vocab import Category
from app.services.analysis import (
    AnalysisConfig,
    AnalysisResult,
    analyze,
    find_redundancy,
    similarity,
)
from tests.engine.fixtures import CLOSET, OUT, WARM, WINTER, A, B, D, S, T, pick, piece

CONFIG = EngineConfig()
CFG = AnalysisConfig()
ALL = list(CLOSET.values())
CASUAL = pick("white_tee", "blue_jeans", "white_sneakers", "black_jeans", "oxford_shirt")


def run(closet: list[ItemView], candidate: ItemView, cfg: AnalysisConfig = CFG) -> AnalysisResult:
    return analyze(closet, candidate, CONFIG, cfg)


# ----------------------------------------------------------------------- golden cases


def test_a_versatile_piece_unlocks_many_outfits() -> None:
    grey_blouse = piece(T, "blouse", "grey", material="cotton")
    result = run(ALL, grey_blouse)
    assert result.kind == "outfit"
    assert result.new_outfits == result.outfits_with_piece >= CFG.strong_unlock
    assert result.verdict == f"Unlocks {result.new_outfits} new outfits, a strong addition"
    assert result.verdict_level == "strong"
    assert result.redundancy.level == "none" and result.missing_slots == ()
    assert result.closet_total_after == result.closet_total_before + result.new_outfits


def test_a_piece_that_unlocks_nothing_explains_what_is_missing() -> None:
    gown = piece(D, "cocktail dress", "black", WINTER)
    closet = pick("white_tee", "blue_jeans", "tan_sandals", "black_jeans")  # summer shoes only
    result = run(closet, gown)
    assert result.new_outfits == 0 and result.outfits_with_piece == 0
    assert result.verdict_level == "blocked"
    assert result.verdict == (
        "You have no shoes that match in season or pattern, so this dress creates no outfits yet"
    )
    assert result.examples == () and result.pairs_with == ()


def test_a_top_without_any_bottoms() -> None:
    result = run(pick("white_sneakers", "brown_loafers"), piece(T, "shirt", "navy"))
    assert result.new_outfits == 0
    assert result.missing_slots == ("You have no bottoms, so this top creates no outfits yet.",)
    assert result.verdict == "You have no bottoms, so this top creates no outfits yet"


def test_a_pair_of_shoes_without_tops_and_bottoms() -> None:
    result = run(pick("floral_dress"), piece(S, "boots", "black"))
    assert result.new_outfits > 0  # a dress is enough to build an outfit
    nothing = run(pick("white_tee"), piece(S, "boots", "black"))
    assert nothing.missing_slots == (
        "You have no bottoms, so this pair of shoes creates no outfits yet.",
    )


def test_a_duplicate_adds_nothing_new() -> None:
    twin = replace(CLOSET["oxford_shirt"], id=9001)
    result = run(ALL, twin)
    assert result.outfits_with_piece > 0  # it makes outfits...
    assert result.new_outfits == 0  # ...but every one already exists with the original
    assert result.redundancy.level == "near-identical"
    assert result.redundancy.item_id == CLOSET["oxford_shirt"].id
    assert result.verdict == "Adds 0 new outfits and duplicates your light blue shirt"
    assert result.verdict_level == "duplicate"
    assert result.closet_total_after == result.closet_total_before


def test_a_duplicate_of_an_accessory() -> None:
    twin = replace(CLOSET["brown_belt"], id=9002)
    result = run(ALL, twin)
    assert result.kind == "accessory" and result.new_outfits == 0
    assert result.verdict == "Adds 0 new outfits and duplicates your brown belt"


def test_a_near_duplicate_with_a_small_difference_still_adds_something() -> None:
    """Same shirt, but warm-weather only: mostly a duplicate, with a few differences."""
    variant = replace(CLOSET["oxford_shirt"], id=9003, seasons=WARM)
    result = run(ALL, variant)
    assert result.redundancy.level in {"near-identical", "similar"}
    assert result.new_outfits <= result.outfits_with_piece


def test_similar_but_not_identical_is_flagged_without_discounting() -> None:
    black_blazer = piece(OUT, "blazer", "black", CLOSET["navy_blazer"].seasons, material="wool")
    result = run(ALL, black_blazer)
    assert result.redundancy.level == "similar"
    assert result.redundancy.description == "navy blazer"
    assert result.new_outfits == result.outfits_with_piece > 0


def test_an_empty_closet() -> None:
    result = run([], piece(T, "shirt", "navy"))
    assert result.verdict_level == "empty" and result.closet_size == 0
    assert "closet first" in result.verdict
    assert result.new_outfits == 0 and result.closet_total_before == 0


def test_an_accessory_finishes_outfits() -> None:
    scarf = piece(A, "scarf", "burgundy", frozenset({"autumn", "winter"}))
    result = run(ALL, scarf)
    assert result.kind == "accessory"
    assert result.new_outfits == result.outfits_with_piece > 0
    assert result.verdict.startswith("Finishes ")
    assert result.closet_total_after == result.closet_total_before  # adds no new outfits
    assert all(scarf.id in e.item_ids for e in result.examples)


def test_an_accessory_with_no_outfit_to_wear_it_with() -> None:
    result = run(pick("white_tee"), piece(A, "belt", "brown"))
    assert result.verdict_level == "blocked"
    assert result.missing_slots == ("You have no complete outfit yet to wear this accessory with.",)


def test_examples_are_diverse_outfits_that_include_the_piece() -> None:
    candidate = piece(T, "shirt", "light blue")
    result = run(ALL, candidate)
    assert 1 <= len(result.examples) <= CFG.examples
    assert all(candidate.id in e.item_ids for e in result.examples)
    assert all(2 <= len(e.reasons) <= 4 for e in result.examples)
    assert all(e.score >= CONFIG.min_score - 1e-9 for e in result.examples)
    assert len({tuple(sorted(e.item_ids)) for e in result.examples}) == len(result.examples)


def test_pairs_with_and_never_pairs_with() -> None:
    sandals = piece(S, "sandals", "tan", frozenset({"summer"}))
    result = run(ALL, sandals)
    pairs = dict(result.pairs_with)
    counts = [n for _, n in result.pairs_with]
    assert counts == sorted(counts, reverse=True) and len(pairs) <= CFG.top_pairs
    assert all(n > 0 for n in counts)
    # a winter coat can never share an outfit with summer sandals
    assert CLOSET["camel_coat"].id in result.never_pairs_with or result.never_pairs_total > 8
    assert CLOSET["camel_coat"].id not in pairs
    # other shoes are not partners, so they are never listed as "never pairs with"
    other_shoes = {CLOSET["white_sneakers"].id, CLOSET["black_boots"].id}
    assert not set(result.never_pairs_with) & other_shoes


def test_never_pairs_with_is_capped_but_counted() -> None:
    sandals = piece(S, "sandals", "tan", frozenset({"summer"}))
    result = run(ALL, sandals, replace(CFG, max_never_pairs=2))
    assert len(result.never_pairs_with) <= 2
    assert result.never_pairs_total >= len(result.never_pairs_with)


def test_a_modest_unlock_with_rarely_used_pieces() -> None:
    """A beige summer skirt that only works with the closet's rarely used pieces."""
    closet = [
        *pick("white_tee", "blue_jeans", "black_jeans", "white_sneakers", "oxford_shirt"),
        *pick("tan_sandals", "floral_dress", "red_skirt"),
    ]
    candidate = piece(B, "skirt", "beige", WARM)
    strict = replace(CFG, strong_unlock=1000, modest_unlock=2, rarely_used_share=0.0)
    result = run(closet, candidate, strict)
    assert result.new_outfits >= 2
    expected = f"Unlocks {result.new_outfits} outfits, mostly with pieces you rarely wear"
    assert result.verdict == expected
    assert result.verdict_level == "mixed"


def test_a_weak_unlock() -> None:
    closet = pick("white_tee", "blue_jeans", "white_sneakers")
    result = run(closet, piece(OUT, "jacket", "olive"))
    assert result.new_outfits == 1 == result.outfits_with_piece
    assert result.verdict == "Unlocks only 1 new outfit"
    assert result.verdict_level == "weak"


def test_verdict_thresholds_are_configurable() -> None:
    candidate = piece(T, "t-shirt", "grey")
    default = run(ALL, candidate)
    picky = run(ALL, candidate, replace(CFG, strong_unlock=10_000, modest_unlock=5_000))
    assert default.verdict_level == "strong"
    assert picky.verdict_level == "weak" and picky.verdict.startswith("Unlocks only")
    assert default.new_outfits == picky.new_outfits  # thresholds change words, not numbers


# --------------------------------------------------------------------- invariants


def test_the_total_after_matches_the_engine_when_there_is_no_duplicate() -> None:
    for candidate in (
        piece(T, "t-shirt", "grey"),
        piece(B, "chinos", "beige"),
        piece(S, "sneakers", "black"),
        piece(OUT, "cardigan", "grey"),
    ):
        result = run(ALL, candidate)
        if result.redundancy.level == "near-identical":
            continue
        engine_after = count_outfits([*ALL, candidate], CONFIG).count
        assert result.closet_total_after == engine_after, candidate


def test_analysis_is_deterministic_and_serializable() -> None:
    candidate = piece(T, "shirt", "light blue")
    first, second = run(ALL, candidate), run(ALL, candidate)
    assert first == second
    assert AnalysisResult.from_dict(first.to_dict()) == first
    import json

    assert AnalysisResult.from_dict(json.loads(json.dumps(first.to_dict()))) == first


def test_the_closet_is_never_mutated_and_the_candidate_is_excluded_from_it() -> None:
    closet = list(ALL)
    candidate = replace(CLOSET["white_tee"], id=9100)
    run(closet, candidate)
    assert closet == ALL
    with_self = run([*ALL, candidate], candidate)  # even if it was passed in by mistake
    assert with_self.closet_size == len(ALL)


# ---------------------------------------------------------------------- redundancy


@pytest.mark.parametrize(
    ("changes", "level"),
    [
        ({}, "near-identical"),
        ({"primary_color": "black"}, "similar"),
        ({"primary_color": "red"}, "none"),
        ({"pattern": "striped"}, "similar"),
    ],
)
def test_redundancy_levels(changes: dict[str, object], level: str) -> None:
    owned = piece(OUT, "blazer", "navy", frozenset({"spring", "autumn"}))
    candidate = replace(owned, id=owned.id + 5000, **changes)  # type: ignore[arg-type]
    assert find_redundancy([owned], candidate, CFG).level == level


def test_different_subtypes_or_categories_are_never_redundant() -> None:
    blazer = piece(OUT, "blazer", "navy")
    jacket = replace(blazer, id=blazer.id + 1, subtype="jacket")
    top = piece(T, "shirt", "navy")
    assert find_redundancy([blazer], jacket, CFG).level == "none"
    assert find_redundancy([blazer], top, CFG).level == "none"


def test_similarity_is_symmetric_and_bounded() -> None:
    a = piece(T, "shirt", "navy")
    b = piece(T, "shirt", "red", WARM)
    assert 0.0 <= similarity(a, b, CFG) <= 1.0
    assert similarity(a, b, CFG) == pytest.approx(similarity(b, a, CFG))
    assert similarity(a, a, CFG) == pytest.approx(1.0)


def test_the_best_match_is_named_not_just_any() -> None:
    near = piece(T, "shirt", "navy")
    far = piece(T, "shirt", "red")
    candidate = replace(near, id=near.id + 7000)
    found = find_redundancy([far, near], candidate, CFG)
    assert found.item_id == near.id and found.description == "navy shirt"


# -------------------------------------------------------------------------- purity


def test_analysis_module_is_pure() -> None:
    source = Path(__file__).resolve().parents[1] / "app" / "services" / "analysis.py"
    banned = ("sqlmodel", "sqlalchemy", "fastapi", "starlette", "httpx", "app.models", "app.db")
    for node in ast.walk(ast.parse(source.read_text())):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            assert not any(name == b or name.startswith(b + ".") for b in banned), name


def test_category_enum_is_the_engines() -> None:
    assert Category.TOP.value == "top"


# ------------------------------------------------------------------------------ docs


def test_documented_thresholds_match_the_code() -> None:
    from dataclasses import fields

    docs = (Path(__file__).resolve().parents[1] / "docs" / "link-analysis.md").read_text()
    for f in fields(AnalysisConfig):
        value = getattr(CFG, f.name)
        rows = [line for line in docs.splitlines() if line.startswith(f"| `{f.name}` |")]
        assert rows, f"{f.name} is missing from docs/link-analysis.md"
        assert any(row.split("|")[2].strip() == str(value) for row in rows), (f.name, value)
