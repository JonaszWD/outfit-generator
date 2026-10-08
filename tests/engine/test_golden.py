"""Golden cases: outfits the engine must accept and outfits it must reject.

Each row is `(case name, pieces from fixtures.CLOSET, context, [expected failing rule])`.
Add rows to record your own opinions. A rejected outfit may fail several rules; the
expected rule only has to be among them. Use "score" for outfits that follow every hard
rule but are too ugly to pass min_score.
"""

import pytest

from app.engine.config import EngineConfig
from app.engine.generator import meets_minimum
from app.engine.rules import failed_rules
from app.engine.scoring import score_outfit
from app.engine.types import Context
from tests.engine.fixtures import pick

CONFIG = EngineConfig()
NONE = Context()

ACCEPTED = [
    # (name, pieces, context)
    ("tee, jeans, sneakers", ("white_tee", "blue_jeans", "white_sneakers"), NONE),
    ("oxford, chinos, loafers", ("oxford_shirt", "beige_chinos", "brown_loafers"), NONE),
    ("polo, chinos, loafers", ("navy_polo", "beige_chinos", "brown_loafers"), NONE),
    (
        "silk blouse, wool trousers, heels",
        ("cream_blouse", "charcoal_trousers", "black_heels"),
        NONE,
    ),
    ("cocktail dress, heels", ("cocktail_dress", "black_heels"), NONE),
    ("cocktail dress, blazer, heels", ("cocktail_dress", "navy_blazer", "black_heels"), NONE),
    ("floral dress, sandals", ("floral_dress", "tan_sandals"), NONE),
    ("floral dress, denim jacket, sandals", ("floral_dress", "denim_jacket", "tan_sandals"), NONE),
    ("hoodie, jeans, sneakers", ("black_hoodie", "black_jeans", "white_sneakers"), NONE),
    ("sweater, jeans, boots", ("grey_sweater", "blue_jeans", "black_boots"), NONE),
    ("striped tee, chinos, sneakers", ("striped_tee", "beige_chinos", "white_sneakers"), NONE),
    (
        "graphic tee + striped trousers (listed pair)",
        ("graphic_tee", "striped_trousers", "white_sneakers"),
        NONE,
    ),
    ("linen shirt, chinos, sandals", ("linen_shirt", "beige_chinos", "tan_sandals"), NONE),
    (
        "cardigan over tee, jeans, boots",
        ("white_tee", "burgundy_cardigan", "blue_jeans", "black_boots"),
        NONE,
    ),
    (
        "blazer over oxford, chinos, loafers",
        ("oxford_shirt", "navy_blazer", "beige_chinos", "brown_loafers"),
        NONE,
    ),
    (
        "coat over sweater, jeans, boots",
        ("grey_sweater", "camel_coat", "blue_jeans", "black_boots"),
        NONE,
    ),
    (
        "one color: green shirt, chinos, sneakers",
        ("green_shirt", "beige_chinos", "white_sneakers"),
        NONE,
    ),
    ("complementary red and blue", ("red_tee", "blue_jeans", "white_sneakers"), NONE),
    ("skirt, tee, sneakers", ("white_tee", "red_skirt", "white_sneakers"), NONE),
    (
        "denim jacket, tee, jeans, sneakers",
        ("white_tee", "denim_jacket", "blue_jeans", "white_sneakers"),
        NONE,
    ),
    ("with belt", ("oxford_shirt", "beige_chinos", "brown_loafers", "brown_belt"), NONE),
    (
        "with belt and watch",
        ("oxford_shirt", "beige_chinos", "brown_loafers", "brown_belt", "silver_watch"),
        NONE,
    ),
    (
        "works for a requested season",
        ("white_tee", "blue_jeans", "white_sneakers"),
        Context(season="summer"),
    ),
]

REJECTED = [
    # (name, pieces, context, rule that must be among the failures, or "score")
    ("no bottom", ("white_tee", "white_sneakers"), NONE, "structure"),
    ("no shoes", ("white_tee", "blue_jeans"), NONE, "structure"),
    ("dress plus jeans", ("floral_dress", "blue_jeans", "tan_sandals"), NONE, "structure"),
    ("two tops", ("white_tee", "oxford_shirt", "blue_jeans", "white_sneakers"), NONE, "structure"),
    (
        "two pairs of shoes",
        ("white_tee", "blue_jeans", "white_sneakers", "brown_loafers"),
        NONE,
        "structure",
    ),
    (
        "two outerwear pieces",
        ("white_tee", "navy_blazer", "olive_jacket", "blue_jeans", "white_sneakers"),
        NONE,
        "structure",
    ),
    (
        "three accessories",
        (
            "oxford_shirt",
            "beige_chinos",
            "brown_loafers",
            "brown_belt",
            "silver_watch",
            "cream_hat",
        ),
        NONE,
        "structure",
    ),
    (
        "two belts",
        ("oxford_shirt", "beige_chinos", "brown_loafers", "brown_belt", "black_belt"),
        NONE,
        "structure",
    ),
    (
        "winter coat with summer sandals",
        ("white_tee", "camel_coat", "blue_jeans", "tan_sandals"),
        NONE,
        "season",
    ),
    (
        "wool sweater in summer",
        ("grey_sweater", "blue_jeans", "boots_allyear"),
        Context(season="summer"),
        "season",
    ),
    (
        "shorts under a coat",
        ("white_tee", "coat_allyear", "shorts_allyear", "white_sneakers"),
        NONE,
        "sanity",
    ),
    (
        "sandals with a coat",
        ("white_tee", "coat_allyear", "blue_jeans", "sandals_allyear"),
        NONE,
        "sanity",
    ),
    ("sweater with shorts", ("grey_sweater", "shorts_allyear", "white_sneakers"), NONE, "sanity"),
    (
        "shorts in a winter-only outfit",
        ("thermal_top", "shorts_allyear", "white_sneakers"),
        NONE,
        "sanity",
    ),
    (
        "sandals in a winter-only outfit",
        ("thermal_top", "blue_jeans", "sandals_allyear"),
        NONE,
        "sanity",
    ),
    (
        "boots in a summer-only outfit",
        ("white_tee", "denim_shorts", "boots_allyear"),
        NONE,
        "sanity",
    ),
    (
        "scarf in a summer-only outfit",
        ("white_tee", "denim_shorts", "white_sneakers", "grey_scarf"),
        NONE,
        "season",
    ),
    (
        "floral with checked",
        ("floral_blouse", "checked_trousers", "white_sneakers"),
        NONE,
        "pattern",
    ),
    ("striped with floral", ("striped_tee", "floral_skirt", "tan_sandals"), NONE, "pattern"),
    ("three non-neutral colors", ("red_tee", "blue_jeans", "green_sneakers"), NONE, "color"),
    ("yellow with green clashes", ("yellow_tee", "green_chinos", "white_sneakers"), NONE, "score"),
]


@pytest.mark.parametrize(("name", "names", "context"), ACCEPTED, ids=[c[0] for c in ACCEPTED])
def test_accepted(name: str, names: tuple[str, ...], context: Context) -> None:
    items = pick(*names)
    assert [r.reason for r in failed_rules(items, context, CONFIG)] == []
    score, breakdown = score_outfit(items, context, CONFIG.scoring)
    assert 0.0 <= score <= 1.0
    assert meets_minimum(score, CONFIG), f"score {score:.2f}: {breakdown}"


@pytest.mark.parametrize(
    ("name", "names", "context", "rule"), REJECTED, ids=[c[0] for c in REJECTED]
)
def test_rejected(name: str, names: tuple[str, ...], context: Context, rule: str) -> None:
    items = pick(*names)
    failures = failed_rules(items, context, CONFIG)
    if rule == "score":
        assert failures == [], "expected only a low score, but a hard rule failed"
        score, _ = score_outfit(items, context, CONFIG.scoring)
        assert not meets_minimum(score, CONFIG), f"score {score:.2f} should be below min_score"
    else:
        assert rule in {r.rule for r in failures}, [r.reason for r in failures]


def test_at_least_fifteen_of_each() -> None:
    assert len(ACCEPTED) >= 15 and len(REJECTED) >= 15
