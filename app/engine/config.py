"""Tunable thresholds for the hard rules and for generation. Weights live in ScoringConfig."""

from dataclasses import dataclass, field

from app.engine.sanity import SANITY_RULES, SanityRule
from app.engine.scoring import ScoringConfig
from app.engine.weather import WeatherConfig


@dataclass(frozen=True)
class EngineConfig:
    # --- hard rules
    max_outerwear: int = 1
    max_accessories: int = 2
    max_non_neutral_colors: int = 2
    # Two non-solid patterns may only meet if the pair is listed here. Solid is always fine.
    allowed_pattern_pairs: frozenset[frozenset[str]] = frozenset(
        {frozenset({"striped", "graphic"})}
    )
    sanity_rules: tuple[SanityRule, ...] = SANITY_RULES
    # Weather: with rain, these shoe subtypes are ruled out and these layers count as rainwear.
    rain_excluded_shoes: frozenset[str] = frozenset({"sandals"})
    rain_outerwear: frozenset[str] = frozenset({"jacket", "coat"})

    # --- generation
    min_score: float = 0.6
    diversity_penalty: float = 0.5  # subtracted per unit of similarity to a chosen outfit
    near_duplicate_similarity: float = 0.75  # Jaccard similarity treated as "the same outfit"
    candidate_pool: int = 400  # top-scoring outfits considered for diverse selection

    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
