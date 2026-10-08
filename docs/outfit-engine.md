# Outfit engine

The engine (`app/engine/`) is pure Python: it has no database, web or ORM imports, which a
test enforces. Routes and services convert DB rows to `ItemView`s at the edge
(`app/services/outfits.py`) and call the engine with an `EngineConfig`.

To tune behavior, change the defaults in `app/engine/config.py` and
`app/engine/scoring.py` (or pass your own `EngineConfig`). A test checks that the numbers in
the tables below match those defaults, so this page cannot silently go stale.

## What counts as an outfit

An **outfit** is one *core look* with at most one layer on top:

- a top and a bottom, **or** a dress,
- plus shoes,
- plus optionally one outerwear piece.

**Accessories** (max 2, no duplicate subtype) finish an outfit but never create a new one, so
30 belts do not multiply the count by 30. They are added to generated suggestions when they
fit. For an accessory, "outfits it appears in" means "outfits it can be worn with".

Only items with **confirmed tags** are used. An item with no seasons is treated as all-season.

## Hard rules (an outfit failing any of these is invalid)

| Rule | Default | Config field | Meaning |
|---|---|---|---|
| Structure | 1 top + 1 bottom or 1 dress, 1 shoes | n/a | Required pieces |
| Outerwear | 1 | `max_outerwear` | At most this many outerwear pieces |
| Accessories | 2 | `max_accessories` | At most this many, no two of the same subtype |
| Shared season | at least 1 | n/a | Every piece must share a season (and the requested season, if any) |
| Non-neutral colors | 2 | `max_non_neutral_colors` | Distinct non-neutral colors (primary and secondary) in the outfit |
| Patterns | one non-solid | `allowed_pattern_pairs` | Two non-solid patterns only if the pair is listed |
| Sanity table | see below | `sanity_rules` | Combinations that make no practical sense |

Allowed pattern pairs (default): `striped + graphic`. Solid goes with anything. Floral, checked
and "other" cannot be combined with another non-solid pattern.

### Sanity table (`app/engine/sanity.py`)

One row per opinion; add a row to add a rule. "Winter-only" means the seasons the whole outfit
shares are a subset of `{winter}`.

| Rejected when the outfit has | Reason shown |
|---|---|
| shorts + coat | Shorts don't go with a coat |
| sandals + coat | Sandals don't go with a coat |
| sweater + shorts | A sweater is too warm for shorts |
| shorts, and the outfit is winter-only | Shorts are not for a winter-only outfit |
| sandals, and the outfit is winter-only | Sandals are not for a winter-only outfit |
| boots, and the outfit is summer-only | Boots are too heavy for a summer-only outfit |
| scarf, and the outfit is summer-only | A scarf is too warm for a summer-only outfit |
| coat, and the outfit is summer-only | A coat is too warm for a summer-only outfit |
| hoodie + coat | A hoodie is too casual under a coat |

## Scoring (soft; 0 to 1)

Each rule gives a value in [0, 1] and a plain-English reason. The score is the **weighted
geometric mean** of the values, so one bad aspect hurts a lot (an arithmetic mean would let
clashing colors hide behind good patterns). Rules that do not apply (weather, when it is not
raining) are left out. An outfit needs `min_score` to be returned.

| Rule | Weight | Value |
|---|---|---|
| `color` | 0.35 | 1.0 with no or one non-neutral color; otherwise the pair's harmony (below) |
| `pattern` | 0.15 | 1.0 with at most one non-solid pattern; `two_patterns_value` for an allowed pair |
| `material` | 0.10 | 1.0, or `material_mismatch_value` when light and heavy fabrics mix |
| `season` | 0.10 | By number of shared seasons: 4 or 3 shared = 1.0, 2 = 0.9, 1 = 0.75 |
| `weather` | 0.1 | Only when it rains: 1.0 with a jacket or coat, `rain_other_layer_value` with another layer, `rain_no_layer_value` with none |

### Color harmony

Neutrals go with everything: black, white, grey, charcoal, navy, beige, cream, brown and tan.
(The first seven are the ones you listed; charcoal and tan are obvious additions.) For two
non-neutral colors the value depends on the hue distance (HSL hue of the palette color):

| Hue distance | Relationship | Value |
|---|---|---|
| 0 to 40 degrees | analogous (or same color) | `analogous_value` 1.0 |
| 150 to 210 (180 +/- 30) | complementary | `complementary_value` 0.9 |
| 100 to 140 (120 +/- 20) | triadic | `triadic_value` 0.6 |
| anything else | clash | `clash_value` 0.15 |

A clash alone drops the score to about 0.5, below the default `min_score`, so it rejects the
outfit. Known weak spot: blue is not in the neutral list, so blue jeans with a green top count
as a clash (80 degrees apart). If you consider denim a neutral, add `blue` and `light blue` to
`neutral_colors`.

## Every threshold and its default

These are the numbers a test compares against the code. Change them in `EngineConfig` and
`ScoringConfig`.

| Field | Default | Meaning |
|---|---|---|
| `max_outerwear` | 1 | See hard rules |
| `max_accessories` | 2 | See hard rules |
| `max_non_neutral_colors` | 2 | See hard rules |
| `min_score` | 0.6 | Minimum score for an outfit to be valid and counted |
| `diversity_penalty` | 0.5 | Subtracted per unit of similarity to an already chosen outfit |
| `near_duplicate_similarity` | 0.75 | Jaccard similarity at which two outfits count as the same |
| `candidate_pool` | 400 | Best-scoring outfits considered when picking diverse results |
| `analogous_max_degrees` | 40.0 | Analogous colors are at most this far apart in hue |
| `complementary_tolerance_degrees` | 30.0 | Complementary means 180 plus or minus this |
| `triadic_tolerance_degrees` | 20.0 | Triadic means 120 plus or minus this |
| `analogous_value` | 1.0 | Color value for analogous colors |
| `complementary_value` | 0.9 | Color value for complementary colors |
| `triadic_value` | 0.6 | Color value for triadic colors |
| `clash_value` | 0.15 | Color value for everything else |
| `two_patterns_value` | 0.6 | Pattern value for an allowed pair of non-solid patterns |
| `material_mismatch_value` | 0.5 | Material value when fabric weights differ by 2 or more |
| `rain_other_layer_value` | 0.85 | Weather value in rain when the layer is not a jacket or coat |
| `rain_no_layer_value` | 0.7 | Weather value in rain with no layer at all |
| `value_floor` | 0.01 | Smallest value used in the geometric mean (avoids log of zero) |

Weights (`ScoringConfig.weights`): `color` 0.35, `pattern` 0.15, `material` 0.1, `season` 0.1,
`weather` 0.1. The last one only applies when it rains.

Material weights used by the material rule: linen 1, silk 1, cotton 2, polyester 2, denim 2,
leather 2, knit 3, wool 3. A spread of 2 or more counts as a mismatch.

## Weather

`app/engine/weather.py` turns a day's forecast into constraints on the `Context`. It is pure:
the forecast comes from a `WeatherProvider` (`app/services/weather.py`) and is converted at the
edge. The day's **representative temperature** is the mean of its high and low. The bands are
`EngineConfig.weather.bands` and can be changed there (a test checks this table).

| Band | Representative temperature | Seasons that suit it | Layer on top |
|---|---|---|---|
| cold | below 5 C | winter | required |
| chilly | 5 to below 12 C | winter, autumn, spring | required |
| mild | 12 to below 18 C | autumn, spring | optional |
| warm | 18 to below 25 C | spring, summer | optional |
| hot | 25 C and above | summer | optional |

- **Seasons.** The outfit's shared seasons must include at least one season that suits the
  band. This is a hard rule (`check_seasons`), alongside any season the user picked by hand.
- **Layer required.** In the cold and chilly bands an outfit must contain an outerwear piece
  (`check_weather`). If nothing in the closet qualifies, the empty-state message says so.
- **Rain.** A day is rainy at a precipitation probability of 50 percent or more
  (`rain_probability_percent`), or 1 mm or more expected (`rain_millimetres`). With rain, shoes in
  `rain_excluded_shoes` (sandals) are ruled out, and the soft `weather` rule prefers a jacket
  or coat (`rain_outerwear`).
- **Combining filters.** A manual season and the weather apply together.
- **Fallback.** If the forecast cannot be loaded (API down, no home city, `WEATHER_PROVIDER=none`)
  the weather is skipped, the page says so, and the user picks a season by hand.
- **Units.** Everything is computed in Celsius; the user's unit setting only changes display.

The provider caches a forecast for `WEATHER_CACHE_MINUTES` (default 60) per location rounded to
2 decimals, in memory per process. A failure is remembered for a minute so a dead API is not
called on every page view.

## Personalization

`app/engine/personalize.py`. Two inputs, both the user's own and both visible on
the preferences page:

**Explicit preferences** (`apply_preferences`). Defaults (strictness 0.5)
reproduce the engine defaults exactly.

| Preference | Effect |
|---|---|
| Color strictness 0 to 1 | Moves `triadic_value` from 0.8 (loose) to 0.4 (strict) and `clash_value` from 0.25 to 0.05, and the non-neutral color cap: 3 below 0.25, 1 above 0.75, otherwise the default 2 |

**Feedback** (`derive`). A thumbs up or down on an outfit is stored as `OutfitFeedback`
(the pieces, the rating, an optional reason, and the rule values it scored). Nothing else is
stored: the adjustment is **derived from the ratings on every request**, deterministically, and
does not depend on their order.

| Effect | How | Cap |
|---|---|---|
| Rule weights | Each rule's weight moves in the direction the ratings point (a disliked outfit with a low color value pushes `color` up), shrunk by `n / (n + 5)` so a few ratings count for little | plus or minus 50 percent of the default weight |
| Reason "colors clash" | Raises the `color` weight | same cap |
| Reason "wrong for weather" | Raises the `season` and `weather` weights; does not penalize the piece pairing | same cap |
| Piece pairs | Every pair of pieces in a disliked outfit gets -0.04, in a liked one +0.02 | each pair between -0.20 and +0.10; one outfit's total between -0.30 and +0.15 |

Rules of the road:

- **Re-rank only.** The base score, validity (`min_score`) and counts never change. The
  candidate pool is 4 times larger so a liked outfit with a slightly lower base score can
  surface, but every outfit still passed the hard rules and reached `min_score` on its base score.
- **Transparent.** A card always shows the base score and, when feedback moved it, "Adjusted
  by your feedback" with the adjusted score and the reason ("Contains a pairing you disliked").
  The preferences page lists the changed weights and the number of boosted and penalized pairs.
- **Reversible.** "Reset personalization" deletes the ratings; with no ratings `derive` returns the
  base configuration unchanged, so the output equals the default output exactly (a test checks
  this with a fixed seed).

## Closet hygiene: what counts as "in the closet"

| State | Suggestions | Counts, stats, candidate analysis | Closet list |
|---|---|---|---|
| Available | yes | yes | yes |
| In the laundry | **no** | **yes** | yes, with a badge |
| Unavailable (lost, lent, donated) | no | no | yes, with a badge, until restored |
| Archived | no | no | hidden; "Archived" filter shows it |

The laundry is a temporary state, so it is skipped only when *suggesting* what to wear. If it
counted for nothing, a piece's "outfits it appears in" and a candidate's "new outfits it adds"
would swing with wash day. Unavailable and archived items are treated as not owned. Archiving is
the primary way to remove an item; deleting is permanent and also removes the photo.

Duplicate warning: the item page runs `find_redundancy` (Phase 4) against the rest of the
closet, laundry included, and warns when a piece is similar or near-identical to another.

## Generation

`generate_outfits(items, context, config, limit, rng_seed, exclude, personalization)`:

1. **Enumerate with early pruning.** Pairwise rules (patterns, shared season)
   are precomputed once as a bitmask per item. Cores, then shoes, then outerwear are combined
   with integer operations, and a partial outfit that breaks a rule (including the color cap
   and the sanity table) is dropped before the rest is added.
2. **Score** what survives (cached per feature combination) and keep those at or above
   `min_score`.
3. **Diverse selection.** The best `candidate_pool` outfits are picked greedily: highest score
   first, then the best `score - diversity_penalty * similarity` to what is already chosen.
   Outfits at or above `near_duplicate_similarity` (Jaccard on item ids) are skipped.
4. **Accessories** are added to each chosen outfit if they pass every hard rule and do not
   lower the score. Ties are broken with the seeded RNG.
5. Reasons (2 to 4) are built from the rule results, not from a language model.

`exclude` takes `Outfit.base_key`s (sorted ids without accessories) and is how "Regenerate"
avoids repeating outfits. The same `rng_seed` gives the same output; without a seed, ties are
broken randomly.

`count_outfits(items, config, must_include=None)` counts the same outfits;
`must_include` may be an item that is not in the closet yet (Phase 4 passes a product).
`closet_stats` returns the total and, per item, how many outfits it appears in.

## Performance

`pytest -m slow tests/engine/test_benchmark.py -s` prints the timings. On a laptop, with 150
items:

| Closet | Valid outfits | Count | Stats | Generate 3 |
|---|---|---|---|---|
| Varied (random colors, patterns, seasons) | about 26,000 | about 0.1 s | about 0.1 s | about 0.1 s |
| Worst case (everything neutral, solid and compatible) | about 456,000 | about 0.5 s | about 0.7 s | about 0.5 s |

## Not in the engine yet

Weather (temperature is carried in `Context` but unused), per-user weights (Phase 5 can pass a
personalized `ScoringConfig` through `get_engine_config`), and learning from feedback.
