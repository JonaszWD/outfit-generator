"""Outfit enumeration, counting, generation and statistics.

An *outfit* is one core look (a top and a bottom, or a dress, plus shoes) with at most one
outerwear piece. Accessories are finishing touches: they never create a new outfit, so a
closet with 30 belts does not multiply the count by 30. They are added to generated
suggestions, and an accessory's "outfit count" is the number of outfits it can be worn with.

Speed: pairwise rules (patterns, shared season) are precomputed once as a
bitmask per item, so each partial outfit is pruned with a few integer operations before it
is ever scored. Scores for repeated feature combinations are cached.
"""

import heapq
import math
import random
from collections import Counter
from collections.abc import Collection, Iterator, Sequence
from itertools import combinations
from typing import NamedTuple

from app.engine import rules, vocab
from app.engine.config import EngineConfig
from app.engine.personalize import Personalization, adjusted_score
from app.engine.scoring import (
    color_value,
    pick_reasons,
    score_outfit,
    season_value,
    weather_value,
)
from app.engine.style import StyleProfile, style_adjustment
from app.engine.types import ClosetStats, Context, CountResult, ItemView, Outfit
from app.engine.vocab import Category

EPS = 1e-9
_SEASON_BIT = {s: 1 << k for k, s in enumerate(vocab.SEASONS)}


def meets_minimum(score: float, config: EngineConfig) -> bool:
    """The one place the min_score comparison is made (tolerant of float rounding)."""
    return score >= config.min_score - EPS


class _Found(NamedTuple):
    score: float
    idxs: tuple[int, ...]  # pool positions, slot order (no accessories)
    shared: int  # season bitmask shared by every piece
    nn: int  # non-neutral color bitmask
    cmask: int  # items pair-compatible with every piece (bitmask over pool positions)
    tags: int  # OR of the pieces' sanity-rule tags


class _Pool:
    """Items usable for a context, with all per-item data as flat lists and bitmasks."""

    def __init__(self, items: Sequence[ItemView], context: Context, config: EngineConfig) -> None:
        self.config = config
        self.context = context
        sc = config.scoring
        acceptable = context.acceptable_seasons
        usable = sorted(
            (
                i
                for i in items
                if (context.season is None or context.season in i.seasons)
                and (acceptable is None or bool(i.seasons & acceptable))
            ),
            key=lambda i: i.id,
        )
        self.accept = 0b1111
        if acceptable is not None:
            self.accept = sum(_SEASON_BIT[s] for s in acceptable if s in _SEASON_BIT)
        self.items = usable
        n = len(usable)
        self.pos = {it.id: k for k, it in enumerate(usable)}
        self.sm = [sum(_SEASON_BIT[s] for s in it.seasons if s in _SEASON_BIT) for it in usable]
        color_bits: dict[str, int] = {}
        self.nn: list[int] = []
        for it in usable:
            mask = 0
            for c in (it.primary_color, *it.secondary_colors):
                if c not in sc.neutral_colors:
                    mask |= 1 << color_bits.setdefault(c, len(color_bits))
            self.nn.append(mask)
        self.color_names = list(color_bits)
        weights_by_item = [sc.material_weights.get(it.material or "", 0) for it in usable]
        self.mat_hi = weights_by_item
        self.mat_lo = [w if w else 99 for w in weights_by_item]  # 99: unknown, never the min
        self.nonsolid = [1 if it.pattern != "solid" else 0 for it in usable]
        # rain: 1 = rainwear (jacket, coat), 2 = another layer, 0 = not outerwear
        self.layer = [
            0
            if it.category != Category.OUTERWEAR
            else (1 if it.subtype in config.rain_outerwear else 2)
            for it in usable
        ]

        # sanity rules: one tag bit per (rule, required matcher)
        self._sanity: list[tuple[int, int | None]] = []
        self.tags = [0] * n
        bit = 0
        for entry in config.sanity_rules:
            need = 0
            for matcher in entry.requires:
                need |= 1 << bit
                for k, it in enumerate(usable):
                    if matcher.matches(it):
                        self.tags[k] |= 1 << bit
                bit += 1
            only = None
            if entry.only_seasons is not None:
                only = sum(_SEASON_BIT[s] for s in entry.only_seasons)
            self._sanity.append((need, only))

        self.by_cat: dict[Category, list[int]] = {c: [] for c in Category}
        for k, it in enumerate(usable):
            self.by_cat[it.category].append(k)
        self.compat = self._build_compat()
        self.acc_mask = sum(1 << k for k in self.by_cat[Category.ACCESSORY])

        self._score_cache: dict[tuple[bool, bool, int, int, int], float] = {}
        self._acc_season: dict[int, int] = {}
        self._acc_color: dict[int, int] = {}
        self._acc_sanity: dict[tuple[int, int], int] = {}
        weights = sc.weights
        self._w = weights
        always = ("color", "pattern", "material", "season")
        self._total_w = sum(weights[r] for r in always)
        self._rain = context.rain
        if context.rain:
            self._total_w += weights["weather"]

    # ---------------------------------------------------------------- pair rules
    def _build_compat(self) -> list[int]:
        n = len(self.items)
        compat = [0] * n
        for i in range(n):
            a = self.items[i]
            for j in range(i + 1, n):
                b = self.items[j]
                if self.sm[i] & self.sm[j] & self.accept and rules.patterns_allowed(
                    a, b, self.config
                ):
                    compat[i] |= 1 << j
                    compat[j] |= 1 << i
        return compat

    def violates(self, tags: int, shared: int) -> bool:
        for need, only in self._sanity:
            if tags & need == need and (only is None or shared & ~only == 0):
                return True
        return False

    # ------------------------------------------------------------------- scoring
    def _ln(self, value: float) -> float:
        return math.log(max(value, self.config.scoring.value_floor))

    def _score_key(self, key: tuple[bool, bool, int, int, int]) -> float:
        """Score from aggregated features: (>1 pattern, mixed weights, shared seasons,
        non-neutral colors, rain layer). Same formula as scoring.score_outfit."""
        multi, mismatch, shared, nn, layer = key
        sc = self.config.scoring
        w = self._w
        names = frozenset(name for k, name in enumerate(self.color_names) if nn >> k & 1)
        total = (
            w["color"] * self._ln(color_value(names, sc))
            + w["pattern"] * self._ln(sc.two_patterns_value if multi else 1.0)
            + w["material"] * self._ln(sc.material_mismatch_value if mismatch else 1.0)
            + w["season"] * self._ln(season_value(shared.bit_count(), sc))
        )
        if self._rain:
            total += w["weather"] * self._ln(weather_value(layer, sc))
        return math.exp(total / self._total_w)

    # --------------------------------------------------------------- enumeration
    def iter_valid(self, must: int | None = None) -> Iterator[_Found]:
        """Every outfit that passes the hard rules and scores at least min_score."""
        cfg = self.config
        cap = cfg.max_non_neutral_colors
        compat, sm, nnm, tg = self.compat, self.sm, self.nn, self.tags
        nsol, mlo, mhi = self.nonsolid, self.mat_lo, self.mat_hi
        violates, score_cache, score_key = self.violates, self._score_cache, self._score_key
        floor = cfg.min_score - EPS
        accept, layers, rain = self.accept, self.layer, self._rain
        tops = self.by_cat[Category.TOP]
        bottoms = self.by_cat[Category.BOTTOM]
        dresses = self.by_cat[Category.DRESS]
        shoes = self.by_cat[Category.SHOES]
        outerwear = self.by_cat[Category.OUTERWEAR]
        outer: list[int | None] = list(outerwear)
        if not self.context.require_outerwear:
            outer.insert(0, None)
        if must is not None:
            kind = self.items[must].category
            if kind == Category.TOP:
                tops, dresses = [must], []
            elif kind == Category.BOTTOM:
                bottoms, dresses = [must], []
            elif kind == Category.DRESS:
                dresses, tops = [must], []
            elif kind == Category.SHOES:
                shoes = [must]
            elif kind == Category.OUTERWEAR:
                outer = [must]
        if rain:  # after the `must` handling, so a required pair of sandals is still ruled out
            shoes = [s for s in shoes if self.items[s].subtype not in cfg.rain_excluded_shoes]

        # a core is (idxs, shared, nn, tags, cmask, nonsolid, lo, hi)
        cores: list[tuple[tuple[int, ...], int, int, int, int, int, int, int]] = []
        for t in tops:
            ct = compat[t]
            for b in bottoms:
                if not ct >> b & 1:
                    continue
                shared, nn, tags = sm[t] & sm[b], nnm[t] | nnm[b], tg[t] | tg[b]
                if not shared & accept or nn.bit_count() > cap or violates(tags, shared):
                    continue
                cores.append(
                    (
                        (t, b),
                        shared,
                        nn,
                        tags,
                        ct & compat[b],
                        nsol[t] + nsol[b],
                        min(mlo[t], mlo[b]),
                        max(mhi[t], mhi[b]),
                    )
                )
        for d in dresses:
            if sm[d] & accept and nnm[d].bit_count() <= cap and not violates(tg[d], sm[d]):
                single = (nsol[d], mlo[d], mhi[d])
                cores.append(((d,), sm[d], nnm[d], tg[d], compat[d], *single))

        for idxs, shared, nn, tags, cm, ns, lo, hi in cores:
            for s in shoes:
                if not cm >> s & 1:
                    continue
                shared2, nn2, tags2 = shared & sm[s], nn | nnm[s], tags | tg[s]
                if not shared2 & accept or nn2.bit_count() > cap or violates(tags2, shared2):
                    continue
                cm2 = cm & compat[s]
                ns2 = ns + nsol[s]
                lo2, hi2 = min(lo, mlo[s]), max(hi, mhi[s])
                for o in outer:
                    if o is None:
                        mixed = hi2 - lo2 >= 2 and hi2 > 0
                        key = (ns2 > 1, mixed, shared2, nn2, 0)
                        score = score_cache.get(key)
                        if score is None:
                            score = score_cache[key] = score_key(key)
                        if score >= floor:
                            yield _Found(score, (*idxs, s), shared2, nn2, cm2, tags2)
                        continue
                    if not cm2 >> o & 1:
                        continue
                    shared3, nn3, tags3 = shared2 & sm[o], nn2 | nnm[o], tags2 | tg[o]
                    if not shared3 & accept or nn3.bit_count() > cap or violates(tags3, shared3):
                        continue
                    lo3, hi3 = min(lo2, mlo[o]), max(hi2, mhi[o])
                    key = (
                        ns2 + nsol[o] > 1,
                        hi3 - lo3 >= 2 and hi3 > 0,
                        shared3,
                        nn3,
                        layers[o] if rain else 0,
                    )
                    score = score_cache.get(key)
                    if score is None:
                        score = score_cache[key] = score_key(key)
                    if score >= floor:
                        yield _Found(score, (*idxs, o, s), shared3, nn3, cm2 & compat[o], tags3)

    # --------------------------------------------------------------- accessories
    def accessories_for(self, found: _Found) -> int:
        """Bitmask of accessories that can be added to this outfit under every hard rule."""
        mask = found.cmask & self.acc_mask
        if not mask:
            return 0
        shared, nn, tags = found.shared, found.nn, found.tags
        if shared not in self._acc_season:
            self._acc_season[shared] = sum(
                1 << a for a in self.by_cat[Category.ACCESSORY] if self.sm[a] & shared & self.accept
            )
        mask &= self._acc_season[shared]
        if nn not in self._acc_color:
            cap = self.config.max_non_neutral_colors
            self._acc_color[nn] = sum(
                1 << a
                for a in self.by_cat[Category.ACCESSORY]
                if (nn | self.nn[a]).bit_count() <= cap
            )
        mask &= self._acc_color[nn]
        key = (tags, shared)
        if key not in self._acc_sanity:
            self._acc_sanity[key] = sum(
                1 << a
                for a in self.by_cat[Category.ACCESSORY]
                if not self.violates(tags | self.tags[a], shared & self.sm[a])
            )
        return mask & self._acc_sanity[key]

    # ------------------------------------------------------------------ outfits
    def view(self, idxs: Sequence[int]) -> list[ItemView]:
        return [self.items[i] for i in idxs]

    def build_outfit(
        self,
        found: _Found,
        accessories: Sequence[int] = (),
        personalization: Personalization | None = None,
        style: StyleProfile | None = None,
    ) -> Outfit:
        base = self.view(found.idxs)
        everything = [*base, *self.view(accessories)]
        rainwear = self.config.rain_outerwear
        score, breakdown = score_outfit(everything, self.context, self.config.scoring, rainwear)
        adjusted: float | None = None
        notes: tuple[str, ...] = ()
        by: tuple[str, ...] = ()
        if personalization is not None and personalization.active:
            tuned, notes = adjusted_score(
                everything, self.context, personalization, score, rainwear
            )
            adjusted = tuned if notes else None
            by = ("feedback",) if notes else ()
        if style is not None and style.active:
            delta, style_notes = style_adjustment(everything, style)
            if style_notes:
                start = score if adjusted is None else adjusted
                adjusted = min(max(start + delta, 0.0), 1.0)
                notes = (*notes, *style_notes)
                by = (*by, "style")
        return Outfit(
            item_ids=tuple(i.id for i in everything),
            base_ids=tuple(i.id for i in base),
            score=score,
            breakdown=breakdown,
            reasons=pick_reasons(breakdown),
            adjusted_score=adjusted,
            adjustment_notes=notes,
            adjusted_by=by,
        )

    def best_accessories(
        self, found: _Found, rng: random.Random, force: int | None = None
    ) -> tuple[int, ...]:
        """Up to max_accessories distinct-subtype accessories that keep or raise the score."""
        mask = self.accessories_for(found)
        options = [a for a in self.by_cat[Category.ACCESSORY] if mask >> a & 1]
        if force is not None:
            options = [a for a in options if a != force]
        base_view = self.view(found.idxs)
        rainwear = self.config.rain_outerwear
        base_score, _ = score_outfit(base_view, self.context, self.config.scoring, rainwear)
        candidates: list[tuple[int, ...]] = [()] if force is None else [(force,)]
        limit = self.config.max_accessories
        for size in range(1, limit + 1):
            for combo in combinations(options, size):
                full = ((force,) if force is not None else ()) + combo
                if len(full) > limit:
                    continue
                if len({self.items[a].subtype for a in full}) != len(full):
                    continue
                candidates.append(full)
        best: tuple[float, float, tuple[int, ...]] | None = None
        for combo in candidates:
            view = [*base_view, *self.view(combo)]
            if rules.failed_rules(view, self.context, self.config):
                continue
            score, _ = score_outfit(view, self.context, self.config.scoring, rainwear)
            if combo != (() if force is None else (force,)) and score < base_score - EPS:
                continue
            if not meets_minimum(score, self.config):
                continue
            key = (round(score, 9), rng.random(), combo)
            if best is None or key[:2] > best[:2]:
                best = key
        return best[2] if best else ((force,) if force is not None else ())


# ----------------------------------------------------------------------- selection


def _similarity(a: Collection[int], b: Collection[int]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


def _select_diverse(candidates: list[_Found], pool: _Pool, limit: int) -> list[_Found]:
    """Greedy: best score first, then best (score - penalty * similarity to chosen)."""
    cfg = pool.config
    chosen: list[_Found] = []
    remaining = list(candidates)  # already best-first
    while remaining and len(chosen) < limit:
        best_at, best_value = -1, -math.inf
        for at, cand in enumerate(remaining):
            sim = max((_similarity(cand.idxs, c.idxs) for c in chosen), default=0.0)
            if sim >= cfg.near_duplicate_similarity:
                continue
            value = cand.score - cfg.diversity_penalty * sim
            if value > best_value + 1e-12:
                best_at, best_value = at, value
        if best_at < 0:
            break
        chosen.append(remaining.pop(best_at))
    return chosen


def _top_candidates(
    pool: _Pool,
    found_iter: Iterator[_Found],
    rng: random.Random,
    size: int,
    skip: Collection[tuple[int, ...]] = (),
) -> list[_Found]:
    heap: list[tuple[float, float, int, _Found]] = []
    for n, found in enumerate(found_iter):
        if skip and tuple(sorted(pool.items[i].id for i in found.idxs)) in skip:
            continue
        entry = (found.score, rng.random(), -n, found)
        if len(heap) < size:
            heapq.heappush(heap, entry)
        elif entry[:3] > heap[0][:3]:
            heapq.heapreplace(heap, entry)
    return [e[3] for e in sorted(heap, reverse=True)]


# ----------------------------------------------------------------------- public API


def enumerate_outfits(
    items: Sequence[ItemView], config: EngineConfig, context: Context | None = None
) -> Iterator[Outfit]:
    """Every valid outfit (without accessories) with its full explanation. Slow; for tests."""
    pool = _Pool(items, context or Context(), config)
    for found in pool.iter_valid():
        yield pool.build_outfit(found)


def generate_outfits(
    items: Sequence[ItemView],
    context: Context,
    config: EngineConfig,
    limit: int,
    rng_seed: int | None = None,
    exclude: Collection[tuple[int, ...]] = (),
    personalization: Personalization | None = None,
    style: StyleProfile | None = None,
) -> list[Outfit]:
    """Up to `limit` good, mutually different outfits, best first.

    `exclude` holds `Outfit.base_key`s that must not be returned (already shown).
    Deterministic for a given `rng_seed`; ties are broken randomly otherwise.

    With `personalization`, validity still comes from the base score (an outfit must reach
    `min_score` as always) but the candidates are *ranked* by the adjusted score. A larger
    candidate pool is used so a liked outfit with a slightly lower base score can surface.

    `style` (reference photos) works the same way: a capped re-rank on top of the base score.
    With `style=None` the result is identical to before.
    """
    rng = random.Random(rng_seed)
    pool = _Pool(items, context, config)
    active = personalization is not None and personalization.active
    styled = style is not None and style.active
    size = max(config.candidate_pool * (4 if active or styled else 1), limit * 20)
    candidates = _top_candidates(pool, pool.iter_valid(), rng, size, frozenset(exclude))
    if active or styled:
        rainwear = config.rain_outerwear
        reranked = []
        for found in candidates:
            view = pool.view(found.idxs)
            tuned = found.score
            if active and personalization is not None:
                tuned, _ = adjusted_score(view, context, personalization, tuned, rainwear)
            if styled and style is not None:
                delta, _ = style_adjustment(view, style)
                tuned = min(max(tuned + delta, 0.0), 1.0)
            reranked.append(found._replace(score=tuned))
        candidates = sorted(reranked, key=lambda f: -f.score)
    chosen = _select_diverse(candidates, pool, limit)
    return [
        pool.build_outfit(
            f,
            pool.best_accessories(f, rng),
            personalization if active else None,
            style if styled else None,
        )
        for f in chosen
    ]


def _containing(pool: _Pool, must: int) -> Iterator[_Found]:
    """Outfits that contain pool item `must`; for an accessory, outfits it can be worn with."""
    if pool.items[must].category == Category.ACCESSORY:
        for found in pool.iter_valid():
            if pool.accessories_for(found) >> must & 1:
                yield found
    else:
        yield from pool.iter_valid(must)


def _pool_with(
    items: Sequence[ItemView], extra: ItemView | None, config: EngineConfig, context: Context
) -> tuple[_Pool, int | None]:
    everything = list(items)
    if extra is not None and all(i.id != extra.id for i in everything):
        everything.append(extra)
    pool = _Pool(everything, context, config)
    return pool, (pool.pos.get(extra.id) if extra is not None else None)


def count_outfits(
    items: Sequence[ItemView],
    config: EngineConfig,
    must_include: ItemView | None = None,
    context: Context | None = None,
    examples: int = 5,
) -> CountResult:
    """How many valid outfits there are, optionally only those containing `must_include`.

    `must_include` need not be part of `items` (Phase 4 passes a product that is not owned
    yet). For an accessory, "contains" means the outfit can take it as a finishing touch.
    """
    pool, must = _pool_with(items, must_include, config, context or Context())
    if must_include is not None and must is None:  # filtered out by the season
        return CountResult(0, ())
    rng = random.Random(0)
    found_iter = pool.iter_valid() if must is None else _containing(pool, must)
    is_accessory = must is not None and pool.items[must].category == Category.ACCESSORY

    count = 0
    heap_size = max(examples * 20, 60)
    heap: list[tuple[float, float, int, _Found]] = []
    for found in found_iter:
        count += 1
        if len(heap) >= heap_size and found.score < heap[0][0]:
            continue  # cheap exit for the vast majority of outfits
        entry = (found.score, rng.random(), -count, found)
        if len(heap) < heap_size:
            heapq.heappush(heap, entry)
        elif entry[:3] > heap[0][:3]:
            heapq.heapreplace(heap, entry)
    top = [e[3] for e in sorted(heap, reverse=True)]
    chosen = _select_diverse(top, pool, examples)
    force = must if is_accessory else None
    shown = tuple(pool.build_outfit(f, pool.best_accessories(f, rng, force=force)) for f in chosen)
    return CountResult(count, shown)


def outfit_keys(
    items: Sequence[ItemView],
    config: EngineConfig,
    must_include: ItemView,
    context: Context | None = None,
) -> set[tuple[int, ...]]:
    """Base keys (sorted ids, no accessories) of the outfits `count_outfits` counts for
    `must_include`. Used to tell which outfits a duplicate piece adds nothing to."""
    pool, must = _pool_with(items, must_include, config, context or Context())
    if must is None:
        return set()
    return {tuple(sorted(pool.items[i].id for i in f.idxs)) for f in _containing(pool, must)}


def companion_counts(
    items: Sequence[ItemView],
    config: EngineConfig,
    must_include: ItemView,
    context: Context | None = None,
) -> dict[int, int]:
    """For every other item, in how many of `must_include`'s outfits it appears (accessories:
    how many of those outfits can take it)."""
    pool, must = _pool_with(items, must_include, config, context or Context())
    counts = dict.fromkeys((i.id for i in pool.items if i.id != must_include.id), 0)
    if must is None:
        return counts
    accessories = pool.by_cat[Category.ACCESSORY]
    for found in _containing(pool, must):
        for i in found.idxs:
            if i != must:
                counts[pool.items[i].id] += 1
        mask = pool.accessories_for(found)
        for a in accessories:
            if a != must and mask >> a & 1:
                counts[pool.items[a].id] += 1
    return counts


def closet_stats(
    items: Sequence[ItemView], config: EngineConfig, context: Context | None = None
) -> ClosetStats:
    """Total valid outfits plus, per item, how many of them it appears in (accessories:
    how many it can be worn with)."""
    pool = _Pool(items, context or Context(), config)
    per = [0] * len(pool.items)
    total = 0
    accessory_groups: Counter[int] = Counter()
    for found in pool.iter_valid():
        total += 1
        for i in found.idxs:
            per[i] += 1
        mask = pool.accessories_for(found)
        if mask:
            accessory_groups[mask] += 1
    for mask, times in accessory_groups.items():
        for a in pool.by_cat[Category.ACCESSORY]:
            if mask >> a & 1:
                per[a] += times
    result = {pool.items[k].id: per[k] for k in range(len(per))}
    for it in items:  # items filtered out by the season contribute nothing
        result.setdefault(it.id, 0)
    return ClosetStats(total, result)


def explain_empty(
    items: Sequence[ItemView], config: EngineConfig, context: Context | None = None
) -> list[str]:
    """Friendly reasons why no outfit can be made, most actionable first."""
    ctx = context or Context()
    season = ctx.season
    usable = [i for i in items if season is None or season in i.seasons]
    if season and items and not usable:
        return [f"Nothing in your closet is tagged for {season}."]
    accept = ctx.acceptable_seasons
    if accept is not None:
        suitable = [i for i in usable if i.seasons & accept]
        if usable and not suitable:
            return ["Nothing in your closet suits today's weather."]
        usable = suitable
        if (
            ctx.require_outerwear
            and usable
            and not any(i.category == Category.OUTERWEAR for i in usable)
        ):
            return ["It is cold today and you have no jacket or coat for it yet."]
    where = f" for {season}" if season else ""
    cats = Counter(i.category for i in usable)
    messages: list[str] = []
    if not cats[Category.SHOES]:
        messages.append(f"No shoes{where} yet.")
    if not cats[Category.DRESS]:
        if not cats[Category.TOP]:
            messages.append(f"No tops{where} yet (or a dress).")
        if not cats[Category.BOTTOM]:
            messages.append(f"No bottoms{where} yet (or a dress).")
    if messages:
        return messages
    pool = _Pool(items, ctx, config)
    tops, bottoms = pool.by_cat[Category.TOP], pool.by_cat[Category.BOTTOM]
    has_core = bool(pool.by_cat[Category.DRESS]) or any(
        pool.compat[t] >> b & 1 for t in tops for b in bottoms
    )
    if not has_core:
        return ["None of your tops and bottoms go together yet (check seasons and patterns)."]
    return ["Your pieces don't combine into good outfits yet. Check that they share a season."]
