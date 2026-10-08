"""Plain data types shared by the outfit engine. No database or web imports."""

from dataclasses import dataclass, field

from app.engine.vocab import SEASONS, Category

ALL_SEASONS: frozenset[str] = frozenset(SEASONS)


@dataclass(frozen=True)
class ItemView:
    """The fields of a closet item that the engine needs. Built from DB rows at the edge."""

    id: int
    category: Category
    subtype: str
    primary_color: str
    secondary_colors: tuple[str, ...] = ()
    pattern: str = "solid"
    seasons: frozenset[str] = ALL_SEASONS  # an untagged item is treated as all-season
    material: str | None = None
    layer_role: str | None = None


@dataclass(frozen=True)
class Context:
    """What the outfit is for. Temperature is carried for Phase 5 and not used yet."""

    season: str | None = None
    temperature_c: float | None = None
    # Set from the weather (see engine/weather.py): the outfit's shared seasons must include at
    # least one of these, a layer on top may be required, and rain rules out open shoes.
    acceptable_seasons: frozenset[str] | None = None
    require_outerwear: bool = False
    rain: bool = False


@dataclass(frozen=True)
class RuleResult:
    """A hard rule's verdict. `reason` explains a failure (or states why it passed)."""

    passed: bool
    reason: str
    rule: str = ""


@dataclass(frozen=True)
class RuleScore:
    """A soft rule's contribution: a 0-1 value and a plain-English reason."""

    rule: str
    value: float
    reason: str


@dataclass(frozen=True)
class Outfit:
    """A scored outfit. `item_ids` is in slot order (top, bottom or dress, outerwear, shoes,
    accessories); `base_ids` is the same without accessories."""

    item_ids: tuple[int, ...]
    base_ids: tuple[int, ...]
    score: float  # the base score: what validity (min_score) and the counts are based on
    breakdown: tuple[RuleScore, ...] = field(default=())
    reasons: tuple[str, ...] = field(default=())
    # Set when personalization re-ranked this outfit. It never replaces the base score.
    adjusted_score: float | None = None
    adjustment_notes: tuple[str, ...] = field(default=())
    adjusted_by: tuple[str, ...] = field(default=())  # "feedback" and/or "style"

    @property
    def rank_score(self) -> float:
        return self.score if self.adjusted_score is None else self.adjusted_score

    @property
    def key(self) -> tuple[int, ...]:
        """Order-independent identity (how outfits are stored and de-duplicated)."""
        return tuple(sorted(self.item_ids))

    @property
    def base_key(self) -> tuple[int, ...]:
        return tuple(sorted(self.base_ids))


@dataclass(frozen=True)
class CountResult:
    count: int
    examples: tuple[Outfit, ...]


@dataclass(frozen=True)
class ClosetStats:
    """`per_item[id]` is the number of valid outfits that item appears in."""

    total: int
    per_item: dict[int, int]
