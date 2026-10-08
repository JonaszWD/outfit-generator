"""Soft scoring: each rule gives a value in [0, 1] and a plain-English reason.

The overall score is the weighted geometric mean of the rule values, so one clearly bad
aspect (clashing colors) pulls the score down far more than an arithmetic mean would.
Rules that do not apply (weather when it is not raining) are left out of the mean.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.engine import vocab
from app.engine.types import ALL_SEASONS, Context, ItemView, RuleScore

RULES = ("color", "pattern", "material", "season", "weather")


def _default_weights() -> dict[str, float]:
    return {
        "color": 0.35,
        "pattern": 0.15,
        "material": 0.10,
        "season": 0.10,
        "weather": 0.10,  # only when it rains
    }


@dataclass(frozen=True)
class ScoringConfig:
    """All weights and thresholds for soft scoring. Phase 5 can personalize an instance."""

    weights: Mapping[str, float] = field(default_factory=_default_weights)
    neutral_colors: frozenset[str] = frozenset(
        {"black", "white", "grey", "charcoal", "navy", "beige", "cream", "brown", "tan"}
    )
    # Color harmony by hue distance between two non-neutral colors (degrees).
    analogous_max_degrees: float = 40.0
    complementary_tolerance_degrees: float = 30.0  # around 180
    triadic_tolerance_degrees: float = 20.0  # around 120
    analogous_value: float = 1.0
    complementary_value: float = 0.9
    triadic_value: float = 0.6
    clash_value: float = 0.15
    # Patterns
    two_patterns_value: float = 0.6
    # Materials: heavier fabrics next to very light ones lose points
    material_weights: Mapping[str, int] = field(
        default_factory=lambda: {
            "linen": 1,
            "silk": 1,
            "cotton": 2,
            "polyester": 2,
            "denim": 2,
            "leather": 2,
            "knit": 3,
            "wool": 3,
        }
    )
    material_mismatch_value: float = 0.5
    # Season: value by how many seasons the whole outfit works in
    season_values: Mapping[int, float] = field(
        default_factory=lambda: {4: 1.0, 3: 1.0, 2: 0.9, 1: 0.75}
    )
    # Weather: with rain, a jacket or coat is best, other layers are fine, no layer is worse
    rain_other_layer_value: float = 0.85
    rain_no_layer_value: float = 0.7
    value_floor: float = 0.01


# ------------------------------------------------------------------ value functions
# These take plain features so the generator can call (and cache) them without building
# ItemViews; `score_outfit` below computes the same features from ItemViews.


def hue_distance(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def color_pair_value(a: str, b: str, cfg: ScoringConfig) -> float:
    if a == b:
        return cfg.analogous_value
    d = hue_distance(vocab.color_hue(a), vocab.color_hue(b))
    if d <= cfg.analogous_max_degrees:
        return cfg.analogous_value
    if abs(d - 180) <= cfg.complementary_tolerance_degrees:
        return cfg.complementary_value
    if abs(d - 120) <= cfg.triadic_tolerance_degrees:
        return cfg.triadic_value
    return cfg.clash_value


def color_value(non_neutral: frozenset[str], cfg: ScoringConfig) -> float:
    """1.0 when there are no or one non-neutral colors, else the worst pair's harmony."""
    colors = sorted(non_neutral)
    if len(colors) <= 1:
        return 1.0
    return min(color_pair_value(a, b, cfg) for i, a in enumerate(colors) for b in colors[i + 1 :])


def pattern_value(non_solid: Sequence[str], cfg: ScoringConfig) -> float:
    return 1.0 if len(non_solid) <= 1 else cfg.two_patterns_value


def material_value(weights: Sequence[int], cfg: ScoringConfig) -> float:
    if len(weights) < 2:
        return 1.0
    return 1.0 if max(weights) - min(weights) <= 1 else cfg.material_mismatch_value


def season_value(shared_count: int, cfg: ScoringConfig) -> float:
    return cfg.season_values.get(shared_count, 0.75)


def weather_value(layer: int, cfg: ScoringConfig) -> float:
    """layer: 0 no outerwear, 1 rainwear (jacket or coat), 2 another layer."""
    return {1: 1.0, 2: cfg.rain_other_layer_value}.get(layer, cfg.rain_no_layer_value)


def combine(values: Mapping[str, float], cfg: ScoringConfig) -> float:
    """Weighted geometric mean over the rules present in `values`."""
    total = sum(cfg.weights[r] for r in values)
    log_sum = sum(cfg.weights[r] * math.log(max(v, cfg.value_floor)) for r, v in values.items())
    return math.exp(log_sum / total)


# ----------------------------------------------------------------------- explanations


def _list(names: Sequence[str], capitalize: bool = True) -> str:
    names = [n.capitalize() if capitalize and i == 0 else n for i, n in enumerate(names)]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _color_reason(
    items: Sequence[ItemView], non_neutral: frozenset[str], cfg: ScoringConfig
) -> str:
    primaries = list(dict.fromkeys(i.primary_color for i in items))
    colors = sorted(non_neutral)
    if not colors:
        if len(primaries) == 1:
            return f"{primaries[0].capitalize()} throughout is a clean, neutral look"
        if len(primaries) <= 4:
            return f"{_list(primaries)} are all neutrals"
        return "Neutral colors throughout"
    if len(colors) == 1:
        neutrals = [c for c in primaries if c in cfg.neutral_colors]
        if neutrals:
            names = _list(neutrals[:3], capitalize=False)
            return f"{colors[0].capitalize()} stands out against neutral {names}"
        return f"{colors[0].capitalize()} is the only color in the outfit"
    a, b = colors[0], colors[1]
    value = color_pair_value(a, b, cfg)
    if value >= cfg.analogous_value:
        return f"{a.capitalize()} and {b} are close on the color wheel"
    if value == cfg.complementary_value:
        return f"{a.capitalize()} and {b} are complementary colors"
    if value == cfg.triadic_value:
        return f"{a.capitalize()} and {b} are a balanced contrast"
    return f"{a.capitalize()} and {b} clash"


def _season_reason(shared: frozenset[str], context: Context) -> str:
    if context.season:
        return f"Suits {context.season}"
    ordered = [s for s in vocab.SEASONS if s in shared]
    if len(ordered) == 4:
        return "Works in every season"
    return f"Suits {_list(ordered, capitalize=False)}"


# --------------------------------------------------------------------------- scoring


def score_outfit(
    items: Sequence[ItemView],
    context: Context,
    cfg: ScoringConfig,
    rain_outerwear: frozenset[str] = frozenset({"jacket", "coat"}),
) -> tuple[float, tuple[RuleScore, ...]]:
    """Score an outfit and explain every rule. `items` should be in slot order."""
    non_neutral = frozenset(
        c
        for i in items
        for c in (i.primary_color, *i.secondary_colors)
        if c not in cfg.neutral_colors
    )
    shared = frozenset.intersection(*(i.seasons for i in items)) if items else ALL_SEASONS
    non_solid = sorted(i.pattern for i in items if i.pattern != "solid")
    known = [cfg.material_weights.get(i.material or "", 0) for i in items]
    weights = [w for w in known if w]

    breakdown = [
        RuleScore("color", color_value(non_neutral, cfg), _color_reason(items, non_neutral, cfg)),
        RuleScore(
            "pattern",
            pattern_value(non_solid, cfg),
            "Solid pieces keep the look clean"
            if not non_solid
            else f"{non_solid[0].capitalize()} is the one statement pattern"
            if len(non_solid) == 1
            else f"{_list(non_solid)} patterns are a known pairing",
        ),
        RuleScore(
            "material",
            material_value(weights, cfg),
            "Fabrics are a similar weight"
            if material_value(weights, cfg) == 1.0
            else "Light and heavy fabrics are mixed",
        ),
        RuleScore("season", season_value(len(shared), cfg), _season_reason(shared, context)),
    ]
    if context.rain:
        layers = [i for i in items if i.category == "outerwear"]
        layer = 0 if not layers else (1 if any(i.subtype in rain_outerwear for i in layers) else 2)
        reason = (
            "A jacket or coat keeps you dry"
            if layer == 1
            else "Some cover for the rain"
            if layer == 2
            else "No rain cover in this outfit"
        )
        breakdown.append(RuleScore("weather", weather_value(layer, cfg), reason))
    score = combine({b.rule: b.value for b in breakdown}, cfg)
    return score, tuple(breakdown)


def pick_reasons(breakdown: Sequence[RuleScore], limit: int = 4) -> tuple[str, ...]:
    """2-4 reasons: color always, then the most informative of the rest."""
    by_rule = {b.rule: b for b in breakdown}
    chosen = [by_rule["color"].reason]
    pattern = by_rule["pattern"]
    if pattern.value < 1.0 or "statement" in pattern.reason:
        chosen.append(pattern.reason)
    if "weather" in by_rule:
        chosen.append(by_rule["weather"].reason)
    chosen.append(by_rule["season"].reason)
    if by_rule["material"].value < 1.0:
        chosen.append(by_rule["material"].reason)
    return tuple(chosen[:limit])
