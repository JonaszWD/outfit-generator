import re

import pytest

from app.engine import vocab
from app.models import Category


def test_every_category_has_subtypes() -> None:
    assert set(vocab.SUBTYPES) == set(Category)
    assert all(vocab.SUBTYPES[c] for c in Category)


def test_subtypes_are_unique_across_categories() -> None:
    all_subtypes = [s for subs in vocab.SUBTYPES.values() for s in subs]
    assert len(all_subtypes) == len(set(all_subtypes))


@pytest.mark.parametrize("category", list(Category))
def test_subtype_must_belong_to_category(category: Category) -> None:
    for own in vocab.SUBTYPES[category]:
        assert vocab.is_valid_subtype(category, own)
    for other in Category:
        if other is not category:
            assert not vocab.is_valid_subtype(category, vocab.SUBTYPES[other][0])


def test_palette_size_and_hex_values() -> None:
    assert 15 <= len(vocab.COLORS) <= 20
    assert len(set(vocab.COLOR_NAMES)) == len(vocab.COLORS)
    for color in vocab.COLORS:
        assert re.fullmatch(r"#[0-9a-f]{6}", color.hex), color


@pytest.mark.parametrize("color", vocab.COLORS, ids=lambda c: c.name)
def test_every_palette_color_maps_to_itself(color: vocab.Color) -> None:
    rgb = (int(color.hex[1:3], 16), int(color.hex[3:5], 16), int(color.hex[5:7], 16))
    assert vocab.nearest_color(rgb) == color.name


@pytest.mark.parametrize(
    ("rgb", "expected"),
    [
        ((0, 0, 0), "black"),
        ((250, 250, 250), "white"),
        ((20, 30, 70), "navy"),
        ((200, 30, 30), "red"),
        ((110, 110, 110), "grey"),
        ((215, 195, 160), "beige"),
        ((60, 120, 60), "green"),
    ],
)
def test_nearest_color_for_arbitrary_rgb(rgb: tuple[int, int, int], expected: str) -> None:
    assert vocab.nearest_color(rgb) == expected


def test_color_distance_is_symmetric_and_zero_for_same() -> None:
    assert vocab.color_distance("navy", "navy") == 0
    assert vocab.color_distance("navy", "red") == vocab.color_distance("red", "navy")
    assert vocab.color_distance("black", "charcoal") < vocab.color_distance("black", "white")
