"""Transparent personalization: preferences and feedback nudge scoring, within hard caps.

Nothing is stored except the user's own inputs (preferences and ratings). The adjusted
configuration is *derived* from them on every request, deterministically, so deleting the
feedback restores the defaults exactly. Personalization re-ranks suggestions; the base score,
the validity rules and the counts are never touched.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import combinations

from app.engine.config import EngineConfig
from app.engine.scoring import RULES, ScoringConfig, score_outfit
from app.engine.types import Context, ItemView

# Why a user may give a thumbs down, and what each reason means for scoring.
REASONS: tuple[str, ...] = ("colors clash", "wrong for weather")


@dataclass(frozen=True)
class FeedbackSample:
    item_ids: tuple[int, ...]
    rating: int  # +1 liked, -1 disliked
    reason: str | None
    rules: Mapping[str, float]  # rule values the outfit scored when it was shown


@dataclass(frozen=True)
class PersonalizationConfig:
    """How far feedback may move things. Every effect is capped."""

    max_weight_change: float = 0.5  # a rule's weight moves at most +/-50%
    shrinkage: float = 5.0  # few ratings count for little: signal / (n + shrinkage)
    signal_gain: float = 2.0
    pair_penalty: float = 0.04  # per dislike of an outfit containing the pair
    pair_bonus: float = 0.02  # per like
    pair_min: float = -0.20
    pair_max: float = 0.10
    outfit_adjustment_min: float = -0.30  # total pair adjustment applied to one outfit
    outfit_adjustment_max: float = 0.15
    noticeable: float = 0.005  # smaller changes are not reported as "adjusted"


@dataclass(frozen=True)
class Personalization:
    scoring: ScoringConfig
    pair_adjustments: Mapping[tuple[int, int], float] = field(default_factory=dict)
    weight_ratios: Mapping[str, float] = field(default_factory=dict)
    samples: int = 0
    config: PersonalizationConfig = PersonalizationConfig()

    @property
    def active(self) -> bool:
        return self.samples > 0 and (
            bool(self.pair_adjustments)
            or any(abs(r - 1.0) > 1e-9 for r in self.weight_ratios.values())
        )


NONE = Personalization(scoring=ScoringConfig())


def apply_preferences(config: EngineConfig, color_strictness: float) -> EngineConfig:
    """The user's explicit settings, applied to the engine configuration.

    Strictness 0.5 reproduces the defaults exactly.
    """
    s = min(max(color_strictness, 0.0), 1.0)
    scoring = replace(
        config.scoring,
        triadic_value=round(0.8 - 0.4 * s, 6),
        clash_value=round(0.25 - 0.2 * s, 6),
    )
    cap = 3 if s < 0.25 else (1 if s > 0.75 else config.max_non_neutral_colors)
    return replace(config, scoring=scoring, max_non_neutral_colors=cap)


def _reason_signals(sample: FeedbackSample) -> dict[str, float]:
    if sample.rating >= 0 or sample.reason is None:
        return {}
    return {
        "colors clash": {"color": 1.0},
        "wrong for weather": {"season": 1.0, "weather": 1.0},
    }.get(sample.reason, {})


def derive(
    base: ScoringConfig,
    samples: Sequence[FeedbackSample],
    pcfg: PersonalizationConfig | None = None,
) -> Personalization:
    """Per-user scoring from feedback. Deterministic and order-independent."""
    pcfg = pcfg or PersonalizationConfig()
    ordered = sorted(samples, key=lambda s: (s.item_ids, s.rating, s.reason or ""))
    n = len(ordered)
    if n == 0:
        return Personalization(scoring=base, config=pcfg)

    signal = dict.fromkeys(RULES, 0.0)
    pairs: dict[tuple[int, int], float] = {}
    for sample in ordered:
        for rule in RULES:
            if rule in sample.rules:
                shortfall = 1.0 - sample.rules[rule]
                signal[rule] += shortfall if sample.rating < 0 else -shortfall
        for rule, strength in _reason_signals(sample).items():
            signal[rule] += strength
        if sample.reason != "wrong for weather":
            delta = -pcfg.pair_penalty if sample.rating < 0 else pcfg.pair_bonus
            for pair in combinations(sorted(sample.item_ids), 2):
                pairs[pair] = pairs.get(pair, 0.0) + delta
    pairs = {
        pair: round(min(max(value, pcfg.pair_min), pcfg.pair_max), 6)
        for pair, value in sorted(pairs.items())
    }

    ratios: dict[str, float] = {}
    weights = dict(base.weights)
    for rule in RULES:
        if rule not in weights:
            continue
        scaled = signal[rule] / (n + pcfg.shrinkage)
        ratio = 1.0 + pcfg.max_weight_change * math.tanh(pcfg.signal_gain * scaled)
        ratio = min(max(ratio, 1 - pcfg.max_weight_change), 1 + pcfg.max_weight_change)
        ratios[rule] = round(ratio, 6)
        weights[rule] = base.weights[rule] * ratios[rule]

    return Personalization(replace(base, weights=weights), pairs, ratios, n, pcfg)


def adjusted_score(
    items: Sequence[ItemView],
    context: Context,
    personalization: Personalization,
    base_score: float,
    rain_outerwear: frozenset[str] = frozenset({"jacket", "coat"}),
) -> tuple[float, tuple[str, ...]]:
    """The outfit's rank score under this user's personalization, and why it moved."""
    if not personalization.active:
        return base_score, ()
    pcfg = personalization.config
    tuned, _ = score_outfit(items, context, personalization.scoring, rain_outerwear)
    ids = sorted(i.id for i in items)
    pair_total = sum(personalization.pair_adjustments.get(p, 0.0) for p in combinations(ids, 2))
    pair_total = min(max(pair_total, pcfg.outfit_adjustment_min), pcfg.outfit_adjustment_max)
    score = min(max(tuned + pair_total, 0.0), 1.0)
    notes: list[str] = []
    if abs(tuned - base_score) >= pcfg.noticeable:
        notes.append("Weighted by what you liked and disliked before")
    if pair_total <= -pcfg.noticeable:
        notes.append("Contains a pairing you disliked")
    elif pair_total >= pcfg.noticeable:
        notes.append("Contains a pairing you liked")
    if abs(score - base_score) < pcfg.noticeable:
        return base_score, ()
    return score, tuple(notes)
