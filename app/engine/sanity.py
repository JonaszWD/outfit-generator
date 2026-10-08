"""Declarative sanity rules: combinations that make no practical sense.

Each rule rejects an outfit that contains items matching *all* of `requires` and, when
`only_seasons` is set, whose shared seasons are a subset of it (e.g. "winter-only").
To add an opinion, add a row; no code changes are needed.
"""

from dataclasses import dataclass

from app.engine.types import ItemView
from app.engine.vocab import Category


@dataclass(frozen=True)
class Match:
    category: Category | None = None
    subtype: str | None = None

    def matches(self, item: ItemView) -> bool:
        return (self.category is None or item.category == self.category) and (
            self.subtype is None or item.subtype == self.subtype
        )


@dataclass(frozen=True)
class SanityRule:
    reason: str
    requires: tuple[Match, ...]
    only_seasons: frozenset[str] | None = None


WINTER = frozenset({"winter"})
SUMMER = frozenset({"summer"})

SHORTS = Match(subtype="shorts")
SANDALS = Match(subtype="sandals")
COAT = Match(subtype="coat")

SANITY_RULES: tuple[SanityRule, ...] = (
    SanityRule("Shorts don't go with a coat", (SHORTS, COAT)),
    SanityRule("Sandals don't go with a coat", (SANDALS, COAT)),
    SanityRule("A sweater is too warm for shorts", (Match(subtype="sweater"), SHORTS)),
    SanityRule("Shorts are not for a winter-only outfit", (SHORTS,), WINTER),
    SanityRule("Sandals are not for a winter-only outfit", (SANDALS,), WINTER),
    SanityRule("Boots are too heavy for a summer-only outfit", (Match(subtype="boots"),), SUMMER),
    SanityRule("A scarf is too warm for a summer-only outfit", (Match(subtype="scarf"),), SUMMER),
    SanityRule("A coat is too warm for a summer-only outfit", (COAT,), SUMMER),
    SanityRule("A hoodie is too casual under a coat", (Match(subtype="hoodie"), COAT)),
)
