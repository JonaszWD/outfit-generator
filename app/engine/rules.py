"""Hard rules: an outfit that fails any of these is invalid, whatever its score.

Every check returns a RuleResult; `failed_rules` runs them all. The generator uses faster
bitmask equivalents of the same rules for pruning (tests assert the two agree).
"""

from collections import Counter
from collections.abc import Sequence
from itertools import combinations

from app.engine.config import EngineConfig
from app.engine.types import ALL_SEASONS, Context, ItemView, RuleResult
from app.engine.vocab import Category


def shared_seasons(items: Sequence[ItemView]) -> frozenset[str]:
    return frozenset.intersection(*(i.seasons for i in items)) if items else ALL_SEASONS


def check_structure(items: Sequence[ItemView], config: EngineConfig) -> RuleResult:
    """(top + bottom) or dress, plus shoes; at most one outerwear and two distinct accessories."""
    rule = "structure"
    count = Counter(i.category for i in items)
    dress, top, bottom = count[Category.DRESS], count[Category.TOP], count[Category.BOTTOM]
    if dress:
        if dress > 1 or top or bottom:
            return RuleResult(False, "A dress replaces the top and bottom; use only one", rule)
    elif top != 1 or bottom != 1:
        return RuleResult(False, "Needs exactly one top and one bottom, or one dress", rule)
    if count[Category.SHOES] != 1:
        return RuleResult(False, "Needs exactly one pair of shoes", rule)
    if count[Category.OUTERWEAR] > config.max_outerwear:
        return RuleResult(False, f"At most {config.max_outerwear} outerwear piece", rule)
    accessories = [i for i in items if i.category == Category.ACCESSORY]
    if len(accessories) > config.max_accessories:
        return RuleResult(False, f"At most {config.max_accessories} accessories", rule)
    if len({a.subtype for a in accessories}) != len(accessories):
        return RuleResult(False, "No duplicate accessory types", rule)
    return RuleResult(True, "Valid structure", rule)


def check_seasons(items: Sequence[ItemView], context: Context) -> RuleResult:
    rule = "season"
    shared = shared_seasons(items)
    if not shared:
        return RuleResult(False, "The pieces have no season in common", rule)
    if context.season and context.season not in shared:
        return RuleResult(False, f"Not every piece suits {context.season}", rule)
    if context.acceptable_seasons is not None and not shared & context.acceptable_seasons:
        return RuleResult(False, "The pieces do not suit today's weather", rule)
    return RuleResult(True, "Shared season", rule)


def check_weather(items: Sequence[ItemView], context: Context, config: EngineConfig) -> RuleResult:
    rule = "weather"
    if context.require_outerwear and not any(i.category == Category.OUTERWEAR for i in items):
        return RuleResult(False, "It is cold enough to need a layer on top", rule)
    if context.rain and any(
        i.category == Category.SHOES and i.subtype in config.rain_excluded_shoes for i in items
    ):
        return RuleResult(False, "Open shoes are a bad idea in the rain", rule)
    return RuleResult(True, "Suits the weather", rule)


def patterns_allowed(a: ItemView, b: ItemView, config: EngineConfig) -> bool:
    if a.pattern == "solid" or b.pattern == "solid":
        return True
    return frozenset({a.pattern, b.pattern}) in config.allowed_pattern_pairs


def check_patterns(items: Sequence[ItemView], config: EngineConfig) -> RuleResult:
    rule = "pattern"
    for a, b in combinations(items, 2):
        if not patterns_allowed(a, b, config):
            return RuleResult(False, f"{a.pattern} and {b.pattern} patterns clash", rule)
    return RuleResult(True, "Patterns are compatible", rule)


def non_neutral_colors(items: Sequence[ItemView], config: EngineConfig) -> frozenset[str]:
    neutral = config.scoring.neutral_colors
    return frozenset(
        c for i in items for c in (i.primary_color, *i.secondary_colors) if c not in neutral
    )


def check_color_cap(items: Sequence[ItemView], config: EngineConfig) -> RuleResult:
    rule = "color"
    if len(non_neutral_colors(items, config)) > config.max_non_neutral_colors:
        return RuleResult(
            False, f"More than {config.max_non_neutral_colors} non-neutral colors", rule
        )
    return RuleResult(True, "Few enough colors", rule)


def check_sanity(items: Sequence[ItemView], config: EngineConfig) -> RuleResult:
    rule = "sanity"
    shared = shared_seasons(items)
    for entry in config.sanity_rules:
        if entry.only_seasons is not None and not shared <= entry.only_seasons:
            continue
        if all(any(m.matches(i) for i in items) for m in entry.requires):
            return RuleResult(False, entry.reason, rule)
    return RuleResult(True, "Nothing impractical", rule)


def check_all(
    items: Sequence[ItemView], context: Context, config: EngineConfig
) -> list[RuleResult]:
    """Every hard rule's result (passed or not), in a fixed order."""
    return [
        check_structure(items, config),
        check_seasons(items, context),
        check_weather(items, context, config),
        check_patterns(items, config),
        check_color_cap(items, config),
        check_sanity(items, config),
    ]


def failed_rules(
    items: Sequence[ItemView], context: Context, config: EngineConfig
) -> list[RuleResult]:
    return [r for r in check_all(items, context, config) if not r.passed]


def accessory_fits(
    base: Sequence[ItemView], accessory: ItemView, context: Context, config: EngineConfig
) -> bool:
    """Can `accessory` be added to a valid base outfit without breaking any hard rule?"""
    return not failed_rules([*base, accessory], context, config)
