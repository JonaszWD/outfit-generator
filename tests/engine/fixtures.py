"""A small hand-written closet for engine tests, and a generator for big ones.

Add your own pieces to CLOSET and your own opinions to test_golden.py.
"""

import random

from app.engine import vocab
from app.engine.types import ALL_SEASONS, ItemView
from app.engine.vocab import Category

WARM = frozenset({"spring", "summer"})
COOL = frozenset({"autumn", "winter"})
MID = frozenset({"spring", "autumn"})
SUMMER = frozenset({"summer"})
WINTER = frozenset({"winter"})

_next_id = iter(range(1, 1000))


def piece(
    category: Category,
    subtype: str,
    color: str,
    seasons: frozenset[str] = ALL_SEASONS,
    pattern: str = "solid",
    material: str | None = None,
    secondary: tuple[str, ...] = (),
) -> ItemView:
    return ItemView(
        id=next(_next_id),
        category=category,
        subtype=subtype,
        primary_color=color,
        secondary_colors=secondary,
        pattern=pattern,
        seasons=seasons,
        material=material,
    )


T, B, D, OUT, S, A = (
    Category.TOP,
    Category.BOTTOM,
    Category.DRESS,
    Category.OUTERWEAR,
    Category.SHOES,
    Category.ACCESSORY,
)

CLOSET: dict[str, ItemView] = {
    # tops
    "white_tee": piece(T, "t-shirt", "white", material="cotton"),
    "red_tee": piece(T, "t-shirt", "red", material="cotton"),
    "yellow_tee": piece(T, "t-shirt", "yellow", material="cotton"),
    "graphic_tee": piece(T, "t-shirt", "black", pattern="graphic", material="cotton"),
    "striped_tee": piece(T, "t-shirt", "navy", pattern="striped", secondary=("white",)),
    "oxford_shirt": piece(T, "shirt", "light blue", material="cotton"),
    "linen_shirt": piece(T, "shirt", "white", WARM, material="linen"),
    "green_shirt": piece(T, "shirt", "green", material="cotton"),
    "floral_blouse": piece(T, "blouse", "pink", MID | SUMMER, pattern="floral"),
    "cream_blouse": piece(T, "blouse", "cream", MID | SUMMER, material="silk"),
    "navy_polo": piece(T, "polo", "navy", WARM | {"autumn"}, material="cotton"),
    "grey_sweater": piece(T, "sweater", "grey", COOL, material="wool"),
    "black_hoodie": piece(T, "hoodie", "black", material="cotton"),
    "thermal_top": piece(T, "t-shirt", "grey", WINTER, material="knit"),
    # bottoms
    "blue_jeans": piece(B, "jeans", "blue", material="denim"),
    "black_jeans": piece(B, "jeans", "black", material="denim"),
    "beige_chinos": piece(B, "chinos", "beige", MID | SUMMER, material="cotton"),
    "green_chinos": piece(B, "chinos", "green", material="cotton"),
    "charcoal_trousers": piece(B, "trousers", "charcoal", COOL | {"spring"}, material="wool"),
    "striped_trousers": piece(B, "trousers", "grey", pattern="striped"),
    "checked_trousers": piece(B, "trousers", "brown", pattern="checked"),
    "floral_skirt": piece(B, "skirt", "pink", WARM, pattern="floral"),
    "red_skirt": piece(B, "skirt", "red"),
    "denim_shorts": piece(B, "shorts", "light blue", SUMMER, material="denim"),
    "shorts_allyear": piece(B, "shorts", "beige"),
    # dresses
    "cocktail_dress": piece(D, "cocktail dress", "black", material="polyester"),
    "floral_dress": piece(D, "casual dress", "pink", WARM, pattern="floral"),
    # outerwear
    "navy_blazer": piece(OUT, "blazer", "navy", MID | WINTER, material="wool"),
    "olive_jacket": piece(OUT, "jacket", "olive", MID),
    "denim_jacket": piece(OUT, "jacket", "light blue", material="denim"),
    "camel_coat": piece(OUT, "coat", "tan", WINTER, material="wool"),
    "coat_allyear": piece(OUT, "coat", "black"),
    "burgundy_cardigan": piece(OUT, "cardigan", "burgundy", COOL | {"spring"}, material="knit"),
    # shoes
    "white_sneakers": piece(S, "sneakers", "white"),
    "green_sneakers": piece(S, "sneakers", "green"),
    "black_boots": piece(S, "boots", "black", COOL | {"spring"}, material="leather"),
    "boots_allyear": piece(S, "boots", "brown", material="leather"),
    "brown_loafers": piece(S, "loafers", "brown", material="leather"),
    "tan_sandals": piece(S, "sandals", "tan", SUMMER, material="leather"),
    "sandals_allyear": piece(S, "sandals", "tan"),
    "black_heels": piece(S, "heels", "black"),
    # accessories
    "brown_belt": piece(A, "belt", "brown", material="leather"),
    "black_belt": piece(A, "belt", "black", material="leather"),
    "grey_scarf": piece(A, "scarf", "grey", COOL, material="wool"),
    "silver_watch": piece(A, "watch", "grey"),
    "cream_hat": piece(A, "hat", "cream"),
}


def pick(*names: str) -> list[ItemView]:
    return [CLOSET[n] for n in names]


def big_closet(n: int = 150, seed: int = 7) -> list[ItemView]:
    """A deterministic closet of n varied items, with closet-like category proportions."""
    rng = random.Random(seed)
    mix = [(T, 0.28), (B, 0.18), (D, 0.06), (OUT, 0.12), (S, 0.14), (A, 0.22)]
    categories = [c for c, w in mix for _ in range(round(n * w))]
    while len(categories) < n:
        categories.append(T)
    items = []
    for k, category in enumerate(categories[:n]):
        seasons = frozenset(rng.sample(vocab.SEASONS, rng.choice([1, 2, 3, 4, 4, 4])))
        items.append(
            ItemView(
                id=k + 1,
                category=category,
                subtype=rng.choice(vocab.SUBTYPES[category]),
                primary_color=rng.choice(vocab.COLOR_NAMES),
                secondary_colors=tuple(rng.sample(vocab.COLOR_NAMES, rng.choice([0, 0, 0, 1]))),
                pattern=rng.choice(["solid"] * 6 + list(vocab.PATTERNS[1:])),
                seasons=seasons,
                material=rng.choice([*vocab.MATERIALS, None]),
            )
        )
    return items


def neutral_closet(n: int = 150, seed: int = 1) -> list[ItemView]:
    """Worst case for the engine: everything neutral, solid, all-season, so almost
    every combination is valid."""
    rng = random.Random(seed)
    mix = [(T, 0.28), (B, 0.18), (D, 0.06), (OUT, 0.12), (S, 0.14), (A, 0.22)]
    categories = [c for c, w in mix for _ in range(round(n * w))]
    neutrals = ["black", "white", "grey", "navy", "beige", "cream", "brown"]
    awkward = {"shorts", "sandals", "coat", "scarf", "boots"}
    items = []
    for k, category in enumerate(categories):
        subtypes = [s for s in vocab.SUBTYPES[category] if s not in awkward]
        items.append(
            ItemView(
                id=k + 1,
                category=category,
                subtype=rng.choice(subtypes or list(vocab.SUBTYPES[category])),
                primary_color=rng.choice(neutrals),
                material="cotton",
            )
        )
    return items
