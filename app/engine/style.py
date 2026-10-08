"""Style references: a taste profile built from inspiration photos, used only to re-rank.

Each reference photo is reduced (by a vision model, outside the engine) to a `StyleSample`:
its main colors, patterns and garment types. `build_profile` merges the samples
into a `StyleProfile`, and `style_adjustment` turns how well an outfit matches that profile
into a small, capped score shift. Like personalization, it never changes validity or counts:
an outfit must still reach `min_score` on its base score, and with no profile nothing moves.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.engine import vocab
from app.engine.types import ItemView


@dataclass(frozen=True)
class StyleConfig:
    max_adjustment: float = 0.2  # the largest shift in either direction
    color_radius: float = 25.0  # CIE76 distance at which a color stops matching
    neutral_point: float = 0.5  # match at which the shift is zero
    noticeable: float = 0.01  # smaller shifts are not reported
    weights: Mapping[str, float] = field(
        default_factory=lambda: {"color": 0.4, "pattern": 0.15, "subtype": 0.15}
    )


@dataclass(frozen=True)
class StyleSample:
    """What one reference photo shows."""

    colors: tuple[str, ...]
    patterns: tuple[str, ...] = ()
    subtypes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StyleProfile:
    """Shares are the fraction of reference photos that show the color, pattern or garment."""

    colors: Mapping[str, float]
    patterns: Mapping[str, float]
    subtypes: Mapping[str, float]
    samples: int

    @property
    def active(self) -> bool:
        return self.samples > 0


def _shares(groups: Sequence[Sequence[str]]) -> dict[str, float]:
    counts: dict[str, int] = {}
    for group in groups:
        for value in set(group):
            counts[value] = counts.get(value, 0) + 1
    return {value: n / len(groups) for value, n in sorted(counts.items())}


def build_profile(samples: Sequence[StyleSample]) -> StyleProfile | None:
    if not samples:
        return None
    return StyleProfile(
        colors=_shares([s.colors for s in samples]),
        patterns=_shares([s.patterns for s in samples]),
        subtypes=_shares([s.subtypes for s in samples]),
        samples=len(samples),
    )


def _color_affinity(
    color: str, profile: StyleProfile, cfg: StyleConfig
) -> tuple[float, str | None]:
    """How well one color matches the palette (0-1) and the palette color it matches best."""
    if not profile.colors:
        return 0.0, None
    top = max(profile.colors.values())
    best, best_name = 0.0, None
    for name, share in profile.colors.items():
        distance = 0.0 if name == color else vocab.color_distance(color, name)
        closeness = max(0.0, 1 - distance / cfg.color_radius)
        value = closeness * share / top
        if value > best:
            best, best_name = value, name
    return best, best_name


def style_match(
    items: Sequence[ItemView], profile: StyleProfile, cfg: StyleConfig | None = None
) -> tuple[float, dict[str, float]]:
    """(overall 0-1 match, the component matches)."""
    cfg = cfg or StyleConfig()
    n = len(items)
    color = sum(_color_affinity(i.primary_color, profile, cfg)[0] for i in items) / n
    if profile.patterns:
        top = max(profile.patterns.values())
        pattern = sum(profile.patterns.get(i.pattern, 0.0) / top for i in items) / n
    else:
        pattern = 0.5
    if profile.subtypes:
        subtype = sum(1 for i in items if i.subtype in profile.subtypes) / n
    else:
        subtype = 0.5
    parts = {"color": color, "pattern": pattern, "subtype": subtype}
    total_weight = sum(cfg.weights.values())
    overall = sum(cfg.weights[k] * v for k, v in parts.items()) / total_weight
    return overall, parts


def style_adjustment(
    items: Sequence[ItemView], profile: StyleProfile, cfg: StyleConfig | None = None
) -> tuple[float, tuple[str, ...]]:
    """The score shift for this outfit (capped at +/- max_adjustment) and why."""
    cfg = cfg or StyleConfig()
    overall, parts = style_match(items, profile, cfg)
    scale = (1 - cfg.neutral_point) if overall >= cfg.neutral_point else cfg.neutral_point
    delta = (overall - cfg.neutral_point) / scale * cfg.max_adjustment
    delta = min(max(delta, -cfg.max_adjustment), cfg.max_adjustment)
    if abs(delta) < cfg.noticeable:
        return 0.0, ()
    notes: list[str] = []
    if delta > 0:
        matched = sorted(
            {
                name
                for i in items
                for value, name in [_color_affinity(i.primary_color, profile, cfg)]
                if name and value >= 0.6
            }
        )
        if parts["color"] >= 0.5 and matched:
            notes.append(f"Colors echo your reference photos ({', '.join(matched)})")
        liked = sorted({i.subtype for i in items if i.subtype in profile.subtypes})
        if liked:
            notes.append(f"Includes pieces like those in your references ({', '.join(liked)})")
        if not notes:
            notes.append("Fits your reference photos")
    else:
        notes.append("Differs from your reference photos")
    return round(delta, 6), tuple(notes)
