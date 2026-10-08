# Link analysis

Paste a product link (or upload a photo) and the app reports how many new outfits the piece
would unlock with your current closet. The piece is a **candidate**: an `Item` with
`source=candidate`, linked from `Candidate.item_id`. Every closet query filters on
`source=owned`, so a candidate never shows up in `/closet`, outfit generation, stats or the
review queue until you press "Add to my closet".

## Flow

1. `POST /candidates` validates the link without any network access, applies the rate limit,
   creates (or reuses) the row and starts `analyze_candidate(candidate_id)` in the background.
2. The job fetches the page through the safe fetcher, extracts the product, downloads the best
   photo, then runs the **same imaging and tagging pipeline as owned items** (EXIF stripped,
   background removed, tagged by the configured `Tagger`).
3. `app/services/analysis.py` (pure, deterministic, engine-based) turns the tags into numbers.
4. `GET /candidates/{id}` polls while the job runs, then shows the result. Editing the tags
   recomputes the analysis. `Accept: application/json` returns the same data as JSON.

`analyze_candidate` has the same shape as `process_item`: a plain function of an id with its own
DB session, an atomic claim (calling it twice is safe, a job stuck for 10 minutes is reclaimed),
failures stored on the row, and a semaphore (`MAX_CONCURRENT_ANALYSES`).

## Safe fetching (`app/services/fetcher.py`)

| Defense | Detail |
|---|---|
| Schemes and URL shape | http and https only; no `user:pass@`; ports 80 and 443 only; at most 2048 characters |
| Address check | The hostname is resolved by us and **every** answer must be a public address. Private, loopback, link-local (including `169.254.169.254`), reserved and multicast ranges are rejected, IPv4 and IPv6, including IPv4-mapped, NAT64 and 6to4 forms. Odd spellings (`2130706433`, `0x7f.1`, `localhost.`) are caught after resolution |
| DNS pinning | The connection goes to the validated IP (the URL is rewritten, the real name stays in the Host header and as the TLS server name), so a second DNS answer cannot redirect it |
| Redirects | Followed by hand, at most 5, and every hop repeats all checks |
| No proxies | `trust_env=False`: a system proxy would bypass the pinning |
| Limits | Connect timeout, total timeout, streamed download aborted at the size limit (HTML: `FETCH_MAX_HTML_KB`, images: 12 MB), content type checked (HTML for pages, JPEG/PNG/WebP/HEIC for images; SVG and GIF refused) |
| Politeness | A clear `User-Agent`, robots.txt honored (cached per host for an hour; a missing file allows everything, an unreachable one disallows, per RFC 9309), and `FETCH_HOST_DELAY_SECONDS` between requests to one host |
| Logging | Only the host is logged, never the path or query string; httpx's own request logging is turned down because it prints full URLs |
| Rate limit | `ANALYSES_PER_HOUR` per user (a Postgres count of the user's recent candidate rows). Finished links re-pasted within the cache window are free; retries count |

`FETCH_ALLOW_PRIVATE_HOSTS=true` is a dev/test switch so a local fixture server can be reached.
It admits loopback and private ranges (and any port) but **never** link-local, cloud-metadata,
unspecified, multicast or reserved addresses. Do not enable it in production.

The optional browser renderer (`SCRAPER_USE_BROWSER=true`, `uv sync --extra browser`) checks
every request the page makes against the same address rules, but a browser resolves DNS itself,
so it cannot pin connections: the DNS-rebinding defense does not apply to it. It is off by
default and not covered by CI.

## Extraction (`app/services/scraper.py`)

Strategies in order of trust, merged field by field (a later one only fills gaps):

1. schema.org `Product` as JSON-LD (via extruct), handling `@graph`, `@type` lists,
   `ProductGroup` with variants, offers as object, list or `AggregateOffer`, images as string,
   list or `ImageObject`; then as microdata.
2. OpenGraph and `product:` meta tags.
3. Heuristics: the page title or `<h1>`, a price in the page, product-looking images, and a color
   and category guessed from the title and description.

A page with no title, no image, no sign of being a product page (a price, or JSON-LD/OpenGraph
product data), or a bot-check page raises `ExtractionFailed`. The UI shows the reason and the
upload-a-photo fallback. Site-specific adapters can be registered in `scraper.ADAPTERS`.

The best image is the first with a reasonable size (shorter side at least 300 px), skipping
logos, sprites, icons, SVG and GIF; otherwise the largest that downloaded.

## What the analysis says

| Field | Meaning |
|---|---|
| `new_outfits` | Valid outfits that contain the piece (accessories: outfits it can finish), **minus** outfits a near-identical piece you own already makes possible |
| `outfits_with_piece` | The same count without that deduction |
| `closet_total_before` / `closet_total_after` | Closet outfit count now, and with the piece (`before + new_outfits`; accessories add none) |
| `examples` | Up to 3 diverse top-scoring outfits that include it, with reasons |
| `pairs_with` | The 5 owned pieces it shares the most outfits with |
| `never_pairs_with` | Owned pieces it could share an outfit with by category but never does (capped list plus total) |
| `redundancy` | The most similar owned piece of the same category and subtype, with level none, similar or near-identical |
| `missing_slots` | Why it makes no outfit at all ("You have no bottoms, so this top creates no outfits yet.") |
| `verdict`, `verdict_level` | One sentence built from the numbers by rule |

The verdict rules, first match wins (`AnalysisConfig` in `app/services/analysis.py`):

1. Empty closet: "Add some items to your closet first".
2. 0 new outfits and a near-identical piece: "Adds 0 new outfits and duplicates your navy blazer".
3. 0 outfits and something missing: the first missing-slot sentence.
4. 0 new outfits otherwise: "Adds no new outfits with what you own".
5. Near-identical with some new outfits: "Mostly duplicates your navy blazer: only N new outfits".
6. At least `strong_unlock`: "Unlocks N new outfits, a strong addition".
7. At least `modest_unlock`: "a solid addition", or "mostly with pieces you rarely wear" when
   at least `rarely_used_share` of its pairings are with rarely used pieces.
8. Otherwise: "Unlocks only N new outfits" (with the same rarely-worn note).

Accessories read "Finishes N of your outfits" with the same thresholds.

### Thresholds

| Field | Default | Meaning |
|---|---|---|
| `strong_unlock` | 20 | New outfits for "a strong addition" |
| `modest_unlock` | 5 | New outfits for "a solid addition" |
| `near_identical_similarity` | 0.9 | Similarity at or above this is a duplicate |
| `similar_similarity` | 0.7 | Similarity at or above this is "similar" |
| `weight_color` | 0.5 | Share of similarity from color closeness |
| `weight_seasons` | 0.25 | ... from season overlap (Jaccard) |
| `weight_pattern` | 0.25 | ... from an identical pattern |
| `color_distance_cap` | 40.0 | Color distance (delta E) at which colors count as unrelated |
| `rarely_used_percentile` | 0.25 | The least-used quarter of the closet counts as "rarely used" |
| `rarely_used_share` | 0.5 | Share of pairings with rarely used pieces that triggers the note |
| `examples` | 3 | Example outfits shown |
| `top_pairs` | 5 | Pieces in "pairs with" |
| `max_never_pairs` | 8 | Pieces listed in "never pairs with" |

Redundancy needs the same category and subtype. The similarity is a weighted sum of color
closeness, season overlap and pattern, so a navy blazer and a black blazer are
"similar" (not duplicates) while two navy blazers of the same seasons are "near-identical".

## Caching and limits

| Setting | Default | Meaning |
|---|---|---|
| `CANDIDATE_CACHE_HOURS` | 24 | A link analyzed within this window is reused, even with different tracking parameters |
| `ANALYSES_PER_HOUR` | 10 | Analyses per user per hour |
| `MAX_CONCURRENT_ANALYSES` | 2 | Analyses running at once per process |
| `CANDIDATE_RETENTION_DAYS` | 30 | Unadopted analyses and their photos are deleted after this long (checked whenever the user starts an analysis) |
| `FETCH_CONNECT_TIMEOUT` / `FETCH_TOTAL_TIMEOUT` | 5 / 15 s | Per request / per fetch including redirects |
| `FETCH_MAX_REDIRECTS` | 5 | Redirect limit |
| `FETCH_MAX_HTML_KB` | 2048 | Largest page read |
| `FETCH_HOST_DELAY_SECONDS` | 1.0 | Pause between requests to one host |
