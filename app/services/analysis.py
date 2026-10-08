"""What would this piece add to the closet? Pure, deterministic logic on top of the engine.

No database, web or network imports. Inputs are engine ItemViews; outputs are plain data
(item ids, counts, sentences) that can be stored as JSON and rendered later. The verdict is
built from the numbers by rule, never by a language model.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.engine import vocab
from app.engine.config import EngineConfig
from app.engine.generator import closet_stats, companion_counts, count_outfits, outfit_keys
from app.engine.rules import patterns_allowed
from app.engine.types import ItemView
from app.engine.vocab import Category

Kind = Literal["outfit", "accessory"]
Level = Literal["none", "similar", "near-identical"]
VerdictLevel = Literal["empty", "blocked", "none", "weak", "mixed", "solid", "strong", "duplicate"]

PARTNERS: dict[Category, frozenset[Category]] = {
    Category.TOP: frozenset(
        {Category.BOTTOM, Category.SHOES, Category.OUTERWEAR, Category.ACCESSORY}
    ),
    Category.BOTTOM: frozenset(
        {Category.TOP, Category.SHOES, Category.OUTERWEAR, Category.ACCESSORY}
    ),
    Category.DRESS: frozenset({Category.SHOES, Category.OUTERWEAR, Category.ACCESSORY}),
    Category.SHOES: frozenset(
        {Category.TOP, Category.BOTTOM, Category.DRESS, Category.OUTERWEAR, Category.ACCESSORY}
    ),
    Category.OUTERWEAR: frozenset(
        {Category.TOP, Category.BOTTOM, Category.DRESS, Category.SHOES, Category.ACCESSORY}
    ),
    Category.ACCESSORY: frozenset(
        {Category.TOP, Category.BOTTOM, Category.DRESS, Category.SHOES, Category.OUTERWEAR}
    ),
}
NOUNS = {
    Category.TOP: "top",
    Category.BOTTOM: "bottom",
    Category.DRESS: "dress",
    Category.SHOES: "pair of shoes",
    Category.OUTERWEAR: "outerwear piece",
    Category.ACCESSORY: "accessory",
}
PLURALS = {
    Category.TOP: "tops",
    Category.BOTTOM: "bottoms",
    Category.DRESS: "dresses",
    Category.SHOES: "shoes",
    Category.OUTERWEAR: "outerwear",
    Category.ACCESSORY: "accessories",
}


@dataclass(frozen=True)
class AnalysisConfig:
    """Verdict thresholds and similarity weights. All numbers are tunable."""

    strong_unlock: int = 20  # at least this many new outfits: "a strong addition"
    modest_unlock: int = 5  # at least this many: "a solid addition"
    near_identical_similarity: float = 0.9
    similar_similarity: float = 0.7
    # similarity = weighted sum of: same-ish color, season overlap, pattern
    weight_color: float = 0.5
    weight_seasons: float = 0.25
    weight_pattern: float = 0.25
    color_distance_cap: float = 40.0  # colors this far apart (delta E) count as unrelated
    # "rarely used" pieces: the least-used quarter of the closet by outfit count
    rarely_used_percentile: float = 0.25
    rarely_used_share: float = 0.5  # share of pairings with rare pieces that triggers the note
    examples: int = 3
    top_pairs: int = 5
    max_never_pairs: int = 8


@dataclass(frozen=True)
class Example:
    item_ids: tuple[int, ...]
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Redundancy:
    level: Level
    item_id: int | None = None
    similarity: float = 0.0
    description: str | None = None  # e.g. "navy blazer"


@dataclass(frozen=True)
class AnalysisResult:
    kind: Kind
    closet_size: int
    outfits_with_piece: int  # every valid outfit that contains the piece
    new_outfits: int  # ... minus those a near-identical piece you own already makes possible
    closet_total_before: int
    closet_total_after: int  # before + new_outfits (accessories finish outfits, adding none)
    examples: tuple[Example, ...]
    pairs_with: tuple[tuple[int, int], ...]  # (owned item id, outfits together), most first
    never_pairs_with: tuple[int, ...]
    never_pairs_total: int
    redundancy: Redundancy
    missing_slots: tuple[str, ...]
    rare_share: float
    verdict: str
    verdict_level: VerdictLevel

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnalysisResult":
        return cls(
            kind=data["kind"],
            closet_size=data["closet_size"],
            outfits_with_piece=data["outfits_with_piece"],
            new_outfits=data["new_outfits"],
            closet_total_before=data["closet_total_before"],
            closet_total_after=data["closet_total_after"],
            examples=tuple(
                Example(tuple(e["item_ids"]), e["score"], tuple(e["reasons"]))
                for e in data["examples"]
            ),
            pairs_with=tuple((int(i), int(n)) for i, n in data["pairs_with"]),
            never_pairs_with=tuple(data["never_pairs_with"]),
            never_pairs_total=data["never_pairs_total"],
            redundancy=Redundancy(**data["redundancy"]),
            missing_slots=tuple(data["missing_slots"]),
            rare_share=data["rare_share"],
            verdict=data["verdict"],
            verdict_level=data["verdict_level"],
        )


# ------------------------------------------------------------------------- redundancy


def describe(item: ItemView) -> str:
    return f"{item.primary_color} {item.subtype}"


def similarity(a: ItemView, b: ItemView, cfg: AnalysisConfig) -> float:
    """0-1 likeness of two pieces of the same category and subtype."""
    distance = vocab.color_distance(a.primary_color, b.primary_color)
    color = max(0.0, 1.0 - distance / cfg.color_distance_cap)
    union = a.seasons | b.seasons
    seasons = len(a.seasons & b.seasons) / len(union) if union else 1.0
    pattern = 1.0 if a.pattern == b.pattern else 0.0
    return cfg.weight_color * color + cfg.weight_seasons * seasons + cfg.weight_pattern * pattern


def find_redundancy(
    closet: Sequence[ItemView], candidate: ItemView, cfg: AnalysisConfig
) -> Redundancy:
    best: tuple[float, ItemView] | None = None
    for item in sorted(closet, key=lambda i: i.id):
        if item.category != candidate.category or item.subtype != candidate.subtype:
            continue
        score = similarity(item, candidate, cfg)
        if best is None or score > best[0] + 1e-12:  # ties keep the lowest id (sorted input)
            best = (score, item)
    if best is None or best[0] < cfg.similar_similarity - 1e-9:
        return Redundancy("none")
    score, item = best
    level: Level = "near-identical" if score >= cfg.near_identical_similarity - 1e-9 else "similar"
    return Redundancy(level, item.id, round(score, 3), describe(item))


# ---------------------------------------------------------------------- missing pieces


def _pair_ok(a: ItemView, b: ItemView, config: EngineConfig) -> bool:
    return bool(a.seasons & b.seasons and patterns_allowed(a, b, config))


def missing_slots(
    closet: Sequence[ItemView], candidate: ItemView, config: EngineConfig
) -> tuple[str, ...]:
    """Why the piece makes no outfit with this closet, as sentences. Empty if it does."""
    noun = NOUNS[candidate.category]
    tail = f"so this {noun} creates no outfits yet."
    by_cat = {c: [i for i in closet if i.category == c] for c in Category}
    needed: list[Category]
    if candidate.category == Category.TOP:
        needed = [Category.BOTTOM, Category.SHOES]
    elif candidate.category == Category.BOTTOM:
        needed = [Category.TOP, Category.SHOES]
    elif candidate.category == Category.DRESS:
        needed = [Category.SHOES]
    elif candidate.category == Category.SHOES:
        has_core = by_cat[Category.DRESS] or (by_cat[Category.TOP] and by_cat[Category.BOTTOM])
        needed = [] if has_core else [c for c in (Category.TOP, Category.BOTTOM) if not by_cat[c]]
        if not has_core:
            return tuple(f"You have no {PLURALS[c]}, {tail}" for c in needed)
    else:  # outerwear and accessories dress up an existing outfit
        if count_outfits(closet, config).count == 0:
            return (f"You have no complete outfit yet to wear this {noun} with.",)
        needed = []
    messages: list[str] = []
    for category in needed:
        if not by_cat[category]:
            messages.append(f"You have no {PLURALS[category]}, {tail}")
        elif not any(_pair_ok(candidate, i, config) for i in by_cat[category]):
            messages.append(
                f"You have no {PLURALS[category]} that match in season or pattern, " + tail
            )
    if not messages:
        messages.append(
            f"This {noun} doesn't combine into a valid outfit with what you own yet "
            "(colors or seasons conflict)."
        )
    return tuple(messages)


# --------------------------------------------------------------------------- verdict


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def verdict(
    kind: Kind,
    new: int,
    raw: int,
    redundancy: Redundancy,
    missing: Sequence[str],
    closet_size: int,
    rare_share: float,
    cfg: AnalysisConfig,
) -> tuple[str, VerdictLevel]:
    if closet_size == 0:
        empty = "Add some items to your closet first, then I can tell you what this unlocks."
        return empty, "empty"
    rare = rare_share >= cfg.rarely_used_share
    dup = redundancy.level == "near-identical"
    if new == 0 and dup:
        return f"Adds 0 new outfits and duplicates your {redundancy.description}", "duplicate"
    if new == 0 and raw == 0 and missing:
        return missing[0].rstrip("."), "blocked"
    if new == 0:
        return "Adds no new outfits with what you own", "none"
    if dup:
        return (
            f"Mostly duplicates your {redundancy.description}: "
            f"only {new} new {_plural(new, 'outfit', 'outfits')}",
            "duplicate",
        )
    if kind == "accessory":
        if new >= cfg.strong_unlock:
            return f"Finishes {new} of your outfits, a strong addition", "strong"
        if new >= cfg.modest_unlock:
            return f"Finishes {new} of your outfits, a solid addition", "solid"
        return f"Finishes only {new} of your {_plural(new, 'outfit', 'outfits')}", "weak"
    if new >= cfg.strong_unlock:
        return f"Unlocks {new} new outfits, a strong addition", "strong"
    if new >= cfg.modest_unlock:
        if rare:
            return f"Unlocks {new} outfits, mostly with pieces you rarely wear", "mixed"
        return f"Unlocks {new} new outfits, a solid addition", "solid"
    text = f"Unlocks only {new} new {_plural(new, 'outfit', 'outfits')}"
    return (text + ", mostly with pieces you rarely wear" if rare else text), "weak"


# ----------------------------------------------------------------------------- analyze


def _swap(key: tuple[int, ...], old: int, new: int) -> tuple[int, ...]:
    return tuple(sorted(new if i == old else i for i in key))


def _rare_ids(usage: dict[int, int], cfg: AnalysisConfig) -> set[int]:
    if not usage:
        return set()
    counts = sorted(usage.values())
    cutoff = counts[int(cfg.rarely_used_percentile * (len(counts) - 1))]
    most = counts[-1]  # when every piece is used equally, none of them is "rarely used"
    return {i for i, n in usage.items() if n <= cutoff and n < most}


def analyze(
    closet: Sequence[ItemView],
    candidate: ItemView,
    config: EngineConfig,
    cfg: AnalysisConfig | None = None,
) -> AnalysisResult:
    """`closet` is what the user owns (never including the candidate)."""
    cfg = cfg or AnalysisConfig()
    owned = [i for i in closet if i.id != candidate.id]
    kind: Kind = "accessory" if candidate.category == Category.ACCESSORY else "outfit"
    redundancy = Redundancy("none")

    if not owned:
        text, level = verdict(kind, 0, 0, redundancy, (), 0, 0.0, cfg)
        return AnalysisResult(
            kind, 0, 0, 0, 0, 0, (), (), (), 0, redundancy,
            ("Your closet is empty. Add some items first.",), 0.0, text, level,
        )  # fmt: skip

    before = count_outfits(owned, config).count
    raw = count_outfits(owned, config, must_include=candidate, examples=cfg.examples)
    redundancy = find_redundancy(owned, candidate, cfg)

    new = raw.count
    twin_id = redundancy.item_id if redundancy.level == "near-identical" else None
    if twin_id is not None:
        twin = next(i for i in owned if i.id == twin_id)
        with_candidate = outfit_keys(owned, config, candidate)
        with_twin = outfit_keys(owned, config, twin)
        if kind == "outfit":  # an outfit is old news if swapping in the twin is already valid
            swapped = (_swap(key, candidate.id, twin_id) for key in with_candidate)
            new = sum(1 for key in swapped if key not in with_twin)
        else:  # an accessory fits the same base outfits as its twin
            new = len(with_candidate - with_twin)

    companions = companion_counts(owned, config, candidate)
    partners = PARTNERS[candidate.category]
    relevant = {i.id: companions.get(i.id, 0) for i in owned if i.category in partners}
    pairs = sorted(((i, n) for i, n in relevant.items() if n > 0), key=lambda p: (-p[1], p[0]))
    never = sorted(i for i, n in relevant.items() if n == 0)

    rare = _rare_ids(closet_stats(owned, config).per_item, cfg)
    total_pairs = sum(n for _, n in pairs)
    rare_share = sum(n for i, n in pairs if i in rare) / total_pairs if total_pairs else 0.0

    missing = missing_slots(owned, candidate, config) if raw.count == 0 else ()
    text, level = verdict(kind, new, raw.count, redundancy, missing, len(owned), rare_share, cfg)
    return AnalysisResult(
        kind=kind,
        closet_size=len(owned),
        outfits_with_piece=raw.count,
        new_outfits=new,
        closet_total_before=before,
        closet_total_after=before + (new if kind == "outfit" else 0),
        examples=tuple(Example(o.item_ids, round(o.score, 4), o.reasons) for o in raw.examples),
        pairs_with=tuple(pairs[: cfg.top_pairs]),
        never_pairs_with=tuple(never[: cfg.max_never_pairs]),
        never_pairs_total=len(never),
        redundancy=redundancy,
        missing_slots=missing,
        rare_share=round(rare_share, 3),
        verdict=text,
        verdict_level=level,
    )
