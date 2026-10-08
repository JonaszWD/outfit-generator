"""Single source of truth for allowed item tag values.

Used by the item form, server-side validation and the closet filters.
"""

from dataclasses import dataclass
from enum import StrEnum


class Category(StrEnum):
    TOP = "top"
    BOTTOM = "bottom"
    DRESS = "dress"
    OUTERWEAR = "outerwear"
    SHOES = "shoes"
    ACCESSORY = "accessory"


SUBTYPES: dict[Category, tuple[str, ...]] = {
    Category.TOP: ("t-shirt", "shirt", "blouse", "sweater", "hoodie", "polo"),
    Category.BOTTOM: ("jeans", "chinos", "trousers", "shorts", "skirt"),
    Category.DRESS: ("casual dress", "cocktail dress", "maxi dress"),
    Category.OUTERWEAR: ("blazer", "jacket", "coat", "cardigan"),
    Category.SHOES: ("sneakers", "boots", "loafers", "sandals", "heels"),
    Category.ACCESSORY: ("belt", "scarf", "hat", "bag", "watch"),
}


@dataclass(frozen=True)
class Color:
    name: str
    hex: str


COLORS: tuple[Color, ...] = (
    Color("black", "#1a1a1a"),
    Color("white", "#ffffff"),
    Color("grey", "#9a9a9a"),
    Color("charcoal", "#444444"),
    Color("navy", "#1f2a44"),
    Color("blue", "#3a6ea5"),
    Color("light blue", "#a9c8e6"),
    Color("green", "#3f7d4e"),
    Color("olive", "#6b6b2f"),
    Color("yellow", "#e8c547"),
    Color("orange", "#e07b39"),
    Color("red", "#c0392b"),
    Color("burgundy", "#6d1f2f"),
    Color("pink", "#e8a0b4"),
    Color("purple", "#7b5ea7"),
    Color("beige", "#d9c7a7"),
    Color("cream", "#f3ebd8"),
    Color("brown", "#6b4a2f"),
    Color("tan", "#b58b5a"),
)
COLOR_NAMES: tuple[str, ...] = tuple(c.name for c in COLORS)
COLOR_HEX: dict[str, str] = {c.name: c.hex for c in COLORS}

OCCASIONS: tuple[str, ...] = ("casual", "work", "formal", "sport", "evening")

PATTERNS: tuple[str, ...] = ("solid", "striped", "checked", "floral", "graphic", "other")
SEASONS: tuple[str, ...] = ("spring", "summer", "autumn", "winter")
LAYER_ROLES: tuple[str, ...] = ("base", "mid", "outer")
MATERIALS: tuple[str, ...] = (
    "cotton",
    "linen",
    "wool",
    "denim",
    "leather",
    "silk",
    "polyester",
    "knit",
)


def _srgb_to_linear(c: float) -> float:
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(r: float, g: float, b: float) -> tuple[float, float, float]:
    """sRGB (0-255) to CIE L*a*b* (D65)."""
    rl, gl, bl = (_srgb_to_linear(v) for v in (r, g, b))
    x = (0.4124564 * rl + 0.3575761 * gl + 0.1804375 * bl) / 0.95047
    y = 0.2126729 * rl + 0.7151522 * gl + 0.0721750 * bl
    z = (0.0193339 * rl + 0.1191920 * gl + 0.9503041 * bl) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


_PALETTE_LAB: dict[str, tuple[float, float, float]] = {
    c.name: rgb_to_lab(*_hex_to_rgb(c.hex)) for c in COLORS
}


def _delta_e(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return float(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5)


def nearest_color(rgb: tuple[int, int, int]) -> str:
    """Name of the palette color closest to an arbitrary RGB value (CIE76 in Lab)."""
    lab = rgb_to_lab(*rgb)
    return min(_PALETTE_LAB, key=lambda name: _delta_e(lab, _PALETTE_LAB[name]))


def color_distance(a: str, b: str) -> float:
    """Perceptual distance (delta E) between two palette colors."""
    return _delta_e(_PALETTE_LAB[a], _PALETTE_LAB[b])


def color_hue(name: str) -> float:
    """HSL hue (degrees, 0-360) of a palette color. Meaningful for non-grey colors."""
    r, g, b = (c / 255 for c in _hex_to_rgb(COLOR_HEX[name]))
    high, low = max(r, g, b), min(r, g, b)
    if high == low:
        return 0.0
    delta = high - low
    if high == r:
        hue = ((g - b) / delta) % 6
    elif high == g:
        hue = (b - r) / delta + 2
    else:
        hue = (r - g) / delta + 4
    return hue * 60


def subtypes_for(category: Category | None) -> tuple[str, ...]:
    return SUBTYPES[category] if category is not None else ()


def is_valid_subtype(category: Category, subtype: str) -> bool:
    return subtype in SUBTYPES[category]


def is_valid_color(name: str) -> bool:
    return name in COLOR_HEX
