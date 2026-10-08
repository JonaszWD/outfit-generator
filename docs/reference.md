# Wardrobe: reference guide

> Detailed, phase-by-phase notes on every feature and setting. It was written as the project grew, so
> where it disagrees with the code or with [outfit-engine.md](outfit-engine.md) (which a test
> keeps in sync with the engine's defaults), trust those. The project overview is in the
> [README](../README.md).

Website where you upload photos of your clothes, get outfit suggestions, and paste a product
link to see how many new outfits that piece would unlock.

**Status: Phase 5 (accounts, personalization, polish).** Create an account, upload photos, get a
background-removed cutout and suggested tags, confirm them, generate scored outfits with
explanations ("Dress me for today" uses the weather), rate outfits, plan your week, and paste a
product link to see how many new outfits that piece would unlock. Deployment: see **[docs/deployment.md](deployment.md)** (Docker, or Vercel on free tiers).

## Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker with Compose
- make

## Quick start

```bash
cp .env.example .env     # set a real POSTGRES_PASSWORD, keep URLs in sync
make install
make up                  # Postgres 16 (creates wardrobe and wardrobe_test)
make migrate
make test
make user EMAIL=you@example.com   # create your account (asks for a password)
make seed EMAIL=you@example.com   # optional: 25 placeholder items for that account
make demo-account        # register, log in, dress for a cold day, rate, log out (in-process)
make demo                # print outfits, the outfit count and the most/least versatile pieces
make demo-link           # end-to-end link analysis against a local fixture shop (no internet)
make dev                 # http://localhost:8000, health at /health
```

If port 5432 is already used on your machine, set `DB_HOST_PORT` (e.g. `5433`) in `.env` and
use the same port in `DATABASE_URL` and `TEST_DATABASE_URL`.

The test database is created only on first start of a fresh volume. If you already have a
volume, run `docker compose exec db sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB -c "CREATE DATABASE wardrobe_test"'`
or `docker compose down -v` (this deletes data).

Run everything in containers instead: `make docker` (database and app; runs migrations, then
Uvicorn with reload on port 8000), `make docker-logs` to follow the log.

**Live development in Docker.** The project folder is bind-mounted into the app container, so
you do not rebuild or restart anything for everyday changes:

| You change | What happens |
|---|---|
| Templates (`app/templates`), CSS, JS (`app/static`) | Visible on the next browser refresh (templates re-read from disk; static files are served with `Cache-Control: no-cache`, so the browser revalidates) |
| Python under `app/` | Uvicorn reloads itself in about a second |
| A migration | `docker compose exec app alembic upgrade head` (or restart the app) |
| `pyproject.toml`, `uv.lock`, `Dockerfile` | `make docker-rebuild` (the only case that needs a rebuild) |

If edits are not noticed (Docker Desktop sometimes does not forward file events), set
`WATCH_POLLING=true` in `.env` and run `make docker`. `.env` is passed to the container;
`DATABASE_URL` is always overridden to point at the `db` container. The long-cache behavior of
static files returns when `ENV=production`.

## Closet (Phase 1)

- `/closet`: grid with "Load more" (HTMX, 24 per batch), item count, and filters for
  category, primary color, season and a text search (name/subtype).
  Filters are plain query params (`/closet?category=shoes&season=summer`), so URLs are
  bookmarkable; invalid values are ignored rather than rejected.
- `/items/new` (batch upload), `/items/{id}`, `/items/{id}/edit`, delete with a confirmation dialog.
- Allowed tag values live in `app/engine/vocab.py` and drive the form, validation and filters.
- Every item query is scoped to the logged-in user; other users' items return 404.
- Items can be **in the laundry** (skipped when suggesting outfits, still counted), **unavailable**
  (out of everything until restored) or **archived** (hidden; the primary way to remove an item).
  "Delete permanently" also removes the photos. The closet's availability filter lists each state.
  The item page warns when a piece duplicates another one you own.

## Link analysis (Phase 4)

Paste a product link at `/analyze` (or upload a photo of the piece). The app reads the page,
tags the garment like one of your own pieces (same cutout and `Tagger`), and reports **how many
new outfits it would unlock** with your closet: the headline number, a one-line verdict, example
outfits with the piece highlighted, what it pairs with and never pairs with, a warning when it
duplicates something you own, and what is missing when it makes no outfit. Editing the tags
recomputes everything. "Add to my closet" adopts the piece; "Discard" deletes it. Past analyses
are listed at `/candidates`. `Accept: application/json` on `/candidates/{id}` returns the result
as JSON. Full details, the verdict rules and every threshold: **[docs/link-analysis.md](link-analysis.md)**.

A candidate is an `Item` with `source=candidate`. Every closet query filters on `source=owned`,
and tests prove it never appears in the closet, stats, outfit generation or the review queue.

### What the scraper does and does not do

- It fetches **only the page you paste** (and that page's product photos), once per analysis,
  for **your personal analysis**. It does not crawl, follow links to other products, log in, or
  fill forms.
- It **respects robots.txt** (cached per host), identifies itself with a clear `User-Agent`,
  and waits between requests to the same host. A site that disallows us gets a clear message
  and the upload-a-photo fallback.
- It reads structured data (schema.org JSON-LD and microdata, OpenGraph tags) and falls back to
  the page title and text. It does not execute JavaScript unless you opt in
  (`SCRAPER_USE_BROWSER`, experimental, see the docs).
- It does **not keep the web page**. Only the product details (title, brand, price, colour,
  image links) and a normalized copy of **one** product photo (EXIF stripped, background removed)
  are stored, so the result can be shown and its tags edited. They stay until you discard the
  analysis or `CANDIDATE_RETENTION_DAYS` passes, unless you add the piece to your closet.
  Product photos are not redistributed.
- With `TAGGER_BACKEND=claude`, the product photo is sent to the tagging API like any other
  photo (see Privacy below).
- **Security:** fetching user-supplied URLs is an SSRF risk, so all fetching goes through
  `app/services/fetcher.py`: only http/https, every resolved address must be public (private,
  loopback, link-local and cloud-metadata ranges are rejected, IPv4 and IPv6), the connection
  is pinned to the validated IP, every redirect is re-checked (at most 5), responses are
  size-limited and content-type checked, and only the host is ever logged. Analyses are
  rate-limited per user. `scripts/demo_link_analysis.py` shows it end to end.

### Link analysis settings

| Variable | Default | Meaning |
|---|---|---|
| `ANALYSES_PER_HOUR` | `10` | Analyses per user per hour (re-pasting a finished link is free) |
| `CANDIDATE_CACHE_HOURS` | `24` | A link analyzed within this window is reused |
| `CANDIDATE_RETENTION_DAYS` | `30` | Unadopted analyses and their photos are deleted after this |
| `MAX_CONCURRENT_ANALYSES` | `2` | Analyses running at once per process |
| `FETCH_CONNECT_TIMEOUT` / `FETCH_TOTAL_TIMEOUT` | `5` / `15` | Seconds per request / per fetch including redirects |
| `FETCH_MAX_REDIRECTS` | `5` | Redirect limit |
| `FETCH_MAX_HTML_KB` | `2048` | Largest page read |
| `FETCH_HOST_DELAY_SECONDS` | `1.0` | Pause between requests to one host |
| `FETCH_ALLOW_PRIVATE_HOSTS` | `false` | **Dev/test only.** Allows loopback and private addresses (never link-local or metadata) so a local fixture server works |
| `SCRAPER_USE_BROWSER` | `false` | Optional Playwright rendering (`uv sync --extra browser`); experimental |

Rate limits now apply to the real account. Still open for Phase 6: the robots/politeness state is
per process (use a shared store when running several workers), and the analysis rate limit is
counted from candidate rows rather than a dedicated counter.

## Style references (prototype)

Add photos of outfits you admire at `/settings/style`. A vision model reads each one into a small
summary (colors, patterns, garments), validated against the closet vocabulary. On the
Outfits page, the **Match my reference photos (AI)** checkbox re-ranks suggestions by how well they
resemble that profile (a capped shift of at most 0.2); leave it unticked for the plain engine, so
you can compare. Valid outfits, base scores and counts never change, and each card says why it
moved ("Colors echo your reference photos (navy)"). Backends (`STYLE_BACKEND`): `fake`, `claude`, `gemini`, `openai` (any OpenAI-compatible host),
or `ollama` for a local model, so photos never leave the machine. Details and setup:
**[docs/style-references.md](style-references.md)**.

## Outfit engine (Phase 3)

`app/engine/` is pure Python (no database or web imports; a test enforces it) and is reused by
the routes and, later, by link analysis. Full rules, thresholds and weights, in tables you can
tune from, are in **[docs/outfit-engine.md](outfit-engine.md)**.

- An **outfit** is a top and bottom (or a dress) plus shoes, with at most one layer on top.
  Accessories finish an outfit but do not multiply the count.
- **Hard rules** (structure, shared season, at most 2 non-neutral colors,
  pattern pairs, a declarative sanity table) decide validity. **Soft scoring** (color harmony,
  pattern balance, materials, season) gives a 0-1 score as a
  weighted geometric mean; the default `min_score` is 0.6.
- `generate_outfits` prunes early with precomputed pairwise compatibility, scores, and picks a
  diverse set. `count_outfits(items, config, must_include=...)` counts valid outfits (Phase 4
  passes a product as `must_include`). `closet_stats` gives per-item contribution.
- Every outfit carries 2-4 reasons built from the rule results ("Navy and beige are both
  neutrals"). A 150-item closet is counted in about 0.1 s (0.5 s in a worst case); see the
  benchmark in the docs.

Pages: `/outfits` (suggest, regenerate without repeats, save, delete, saved list) and
`/closet/stats` (total valid outfits, most and least versatile pieces; `Accept:
application/json` returns JSON). Suggestions are stored (unsaved) so they have an id; saving
sets `favorite`, `(user_id, item_ids)` is unique with `item_ids` sorted, unsaved suggestions
older than a day are pruned, and deleting an item removes outfits that use it. Only
confirmed items take part.

## Auto-tagging (Phase 2)

1. **Upload** (`/items/new`, several photos at once): each file is validated by content
   (JPEG, PNG, WebP, HEIC), EXIF-rotated, resized to 1600px and **stripped of all metadata,
   including GPS**. That normalized copy is the "original": the raw upload is never stored.
   One `pending` item is created per file and the request returns immediately.
2. **Pipeline** (`app/services/pipeline.py`, `process_item(item_id)`), run as a background task:
   `pending → processing → done | failed`. It removes the background with rembg (transparent
   PNG cutout, model loaded once per process), computes dominant colors from the cutout's
   opaque pixels, asks the tagger for tags, and cross-checks the model's primary color against
   the pixels. A disagreement lowers that field's confidence and adds a flag; neither answer is
   overridden. If background removal fails, the item is still tagged (the original is shown).
   Failures are stored in `tagging_error`; the card offers **Retry**.
3. **Review** (`/items/{id}/review`): cutout next to the form pre-filled with the AI tags.
   Fields under `TAG_CONFIDENCE_THRESHOLD` are outlined and marked "Please check". Nothing
   counts until **Confirm** (`POST /items/{id}/confirm`), which validates against the
   vocabulary and sets `tags_confirmed`. The AI output stays in `ai_tags` / `ai_confidence`
   (JSONB) for debugging and accuracy measurement; the item's own columns hold only confirmed tags.
4. **Closet**: cards poll `/items/{id}/status` every 2s only while pending or processing. A
   "Needs review" filter and a banner list unconfirmed items; **Confirm all** is enabled only
   when none of them has a low-confidence field.

The pipeline function takes only an id and builds its own DB session and dependencies, so it can
move to Celery or RQ unchanged. Concurrency is capped by `MAX_CONCURRENT_PIPELINES`
(a semaphore around the CPU-heavy work). Transient API errors (429, 5xx, connection) are retried
with exponential backoff by the Anthropic SDK (`max_retries`); output that fails validation is
retried once with the error appended, then the item is marked failed.

### Privacy

With `TAGGER_BACKEND=claude` or `gemini`, **uploaded photos are sent to a third-party API (Anthropic or Google)** for
tagging. EXIF (including GPS) is stripped before any image leaves the server, because only the
normalized copy is ever used. With `TAGGER_BACKEND=fake` (the default) nothing leaves the
machine. The API key is a secret setting and is never logged, nor are image bytes.

### Measuring accuracy

```bash
uv run python -m scripts.eval_tagger photos/ --labels photos/labels.csv
```

`labels.csv` has a `filename` column plus any of `category,subtype,primary_color,pattern`
(blank cells are skipped). It prints per-field accuracy,
mean confidence when right vs. wrong (useful for choosing the threshold), and every mismatch.
It uses the tagger selected by `TAGGER_BACKEND` and the same normalization as the pipeline.

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `DEV_USER_EMAIL` | `dev@example.com` | Email of the seeded development user |
| `MEDIA_ROOT` | `media` | Directory for uploaded images |
| `MAX_UPLOAD_MB` | `10` | Max size per uploaded file |
| `MAX_BATCH_FILES` | `10` | Max files per upload request |
| `TAGGER_BACKEND` | `fake` | `fake` (offline), `claude` or `gemini` (needs `GEMINI_API_KEY`, model `GEMINI_MODEL`) |
| `TAGGER_MODEL` | `claude-opus-5-5` | Model used when the backend is `claude` |
| `ANTHROPIC_API_KEY` | (empty) | Required for the `claude` backend |
| `TAG_CONFIDENCE_THRESHOLD` | `0.7` | Fields below this are highlighted for review |
| `MAX_CONCURRENT_PIPELINES` | `2` | Parallel cutout+tagging jobs per process |
| `REMBG_MODEL` | `u2net` | rembg model (`u2netp` is smaller and faster) |

### Storage layout

`MEDIA_ROOT/<user_id>/<uuid>.jpg` (max 1600px long side), `<uuid>_thumb.jpg` (400px),
`<uuid>_cutout.png` and `<uuid>_cutout_thumb.png` (transparent cutout, once tagged).
`Item.image_path` and `original_image_path` hold the original's key; the other keys are derived
from it, and all four files are deleted with the item. The cutout is what the UI shows.
Uploads are identified by content (JPEG/PNG/WebP/HEIC via Pillow, not extension or Content-Type),
size-limited, EXIF-rotated, stripped of all metadata (including GPS) and re-encoded as JPEG.
Filenames are random; the client's filename is never used.

Code only talks to the `Storage` protocol (`app/services/storage.py`), so an S3-compatible
implementation can replace `LocalDiskStorage` later.

Media is served by `GET /media/<user_id>/<file>`, a route that checks the requested
`user_id` against the current user (404 otherwise) and validates the filename pattern. It is
not a public static mount, so the files stay private. Starlette spools an upload to disk
before the size check runs; put a request-size limit on the reverse proxy in production.

### Seeding

`make seed` creates 25 placeholder items (solid color images). It is idempotent: items are
matched by name. It fills an **existing account**: `make seed EMAIL=you@example.com`. Without
`EMAIL` it uses `DEV_USER_EMAIL`, creating that (passwordless) account if needed.

The dev user from earlier phases has no password. To keep its items and log in, claim it:
`make user EMAIL=dev@example.com` sets a password on a passwordless account (an account that
already has a password is never changed by the script).

### Tests

`make test` skips tests marked `slow` (the real rembg model and the engine benchmark). Run them
with `uv run pytest -m slow -s`.

## Accounts and security (Phase 5)

**Setup.** Register at `/register`, or `make user EMAIL=...`. Set these in `.env` (all in
`.env.example`):

| Setting | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | dev value | Signs CSRF tokens. **Required to be private when `ENV=production`** (the app refuses to start otherwise). `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `COOKIE_SECURE` | unset | `true`/`false`. Unset means secure when `ENV=production`. Also turns on HSTS |
| `SESSION_DAYS` | 14 | Login lifetime |
| `MIN_PASSWORD_LENGTH` | 10 | Plus a common-password list and a no-email-in-password check |
| `APP_BASE_URL` | `http://localhost:8000` | Used in password-reset links |
| `EMAIL_BACKEND` | `console` | `console` prints mail (reset links appear in the server log); `smtp` sends it |
| `EMAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_STARTTLS` | | SMTP delivery |
| `LOGIN_ATTEMPTS_PER_ACCOUNT` / `_PER_IP`, `LOGIN_WINDOW_MINUTES` | 5 / 20 / 15 | Failed logins per window |
| `RESET_REQUESTS_PER_ACCOUNT` / `_PER_IP` | 3 / 10 | Reset emails per hour |
| `REGISTER_PER_IP` | 10 | Registrations per hour |
| `WEATHER_PROVIDER` | `open-meteo` | `none` disables every outside weather call |
| `WEATHER_CACHE_MINUTES`, `WEATHER_TIMEOUT_SECONDS` | 60, 5 | Forecast cache and timeout |

**Sessions.** Server-side, in Postgres. The cookie (`wardrobe_session`: HttpOnly, SameSite=Lax,
Secure per the setting) holds a random 256-bit token; only its SHA-256 is stored, so a leaked
database cannot be replayed as cookies. A new session is created on every login (the presented
one is revoked), and sessions expire after `SESSION_DAYS`. Log out, "Log out everywhere", a
password change and a password reset all revoke sessions. Passwords use argon2id.

**Registration and reset do not reveal whether an email exists.** Registering an existing email
looks exactly like registering a new one (the owner gets an email instead). "Forgot password"
always answers the same. Reset links are single-use, stored hashed, and expire after 1 hour.
Login, registration and reset are rate limited per IP and per account with a Postgres counter
(only hashes of IPs and emails are stored). Wrong-password and unknown-account logins take the
same time.

**Every route needs a login** except a short public list (`/`, `/health`, login, register,
forgot, reset, static files). New routers are protected by default (`app/main.py`), and
`tests/test_route_auth.py` walks every route and fails if one outside the list answers without a
session.

**CSRF.** Logged in, the token is an HMAC of the session cookie (a token from another session
never validates). Logged out (login, register, reset), a double-submit cookie also covers login
CSRF. HTMX sends it as `X-CSRF-Token` through `hx-headers` on `<body>`; forms carry it as a
hidden field.

**Headers.** CSP (`default-src 'none'`; scripts only from `'self'`, no inline script, no eval;
styles from `'self'` and Google Fonts; `style-src-attr` allows only `style=""` attributes for
color swatches), `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
`Permissions-Policy`, COOP/CORP, `Cache-Control: no-store` on HTML/JSON, HSTS when cookies are
secure. There is no CORS middleware, so cross-origin requests get no CORS headers. htmx is
vendored in `app/static/vendor/`, which is why scripts need no CDN. A test crawls every page and
checks it against the CSP.

**Logging.** One JSON line per request: method, route *template*, status, duration, user id.
Never paths with ids or tokens, query strings, emails, IP addresses or form values. A test
checks this.

## Weather and preferences (Phase 5)

Set a home city at `/settings/preferences` (searched through Open-Meteo's geocoder). The
outfit page shows **Today's weather** and a **Dress me for today** button; a
manual season and the weather combine. Temperature bands, rain handling and the fallback are in
**[docs/outfit-engine.md](outfit-engine.md#weather)** (for example below 12 C requires a
layer on top; rain rules out sandals and prefers a jacket). If the weather API is down the page
says so and you pick a season by hand. Preferences also hold a color-strictness
slider and Celsius/Fahrenheit.

## Personalization (Phase 5)

Like or dislike an outfit (with an optional reason). Feedback re-ranks suggestions through small,
capped, deterministic adjustments; cards always show the base score, plus "Adjusted by your
feedback" when it moved. `/settings/preferences` lists what changed and has **Reset
personalization**, which deletes the ratings and restores the default output exactly. Limits and
formulas: [docs/outfit-engine.md](outfit-engine.md#personalization).

## Planner and saved outfits (Phase 5)

Saved outfits carry occasion tags and can be filtered by occasion. `/planner` assigns a saved
outfit to each day of a week and warns when a piece repeats on consecutive days, when the
forecast does not suit the outfit, and when a piece is in the laundry, unavailable or archived.

## Privacy (Phase 5)

- **Third parties.** The only data that leaves the server: the city name you type (to find its
  coordinates) and your home coordinates **rounded to 2 decimals** (about 1 km), sent to
  Open-Meteo for the forecast. With `WEATHER_PROVIDER=none` nothing is sent. Photos go to the
  Anthropic API only if you set `TAGGER_BACKEND=claude` (see Auto-tagging), and product links
  you paste are fetched by the server.
- **Export.** `/account/export` downloads a zip: `data.json` (account, preferences, items,
  outfits, feedback, plans, analyses) and every photo. It holds only your rows, and never your
  password hash.
- **Delete.** `/account/delete` (password and typing DELETE) removes your rows (everything
  cascades from the user row), then every stored file, including stray files in your folder.
- **Accessibility.** See [docs/accessibility.md](accessibility.md).

## Make targets

`install`, `up`, `down`, `docker`, `docker-rebuild`, `docker-logs`, `migrate`, `downgrade`, `revision m="message"`, `test`, `lint`,
`format`, `typecheck`, `dev`, `seed` (`EMAIL=`), `user` (`EMAIL=`), `demo`, `demo-link`,
`demo-account`.

## Layout

```
app/
  main.py        app factory, router registration
  config.py      settings from environment (pydantic-settings)
  db.py          engine, session dependency
  models.py      SQLModel tables: users, sessions, items, outfits, feedback, plans, candidates
  deps.py        get_current_user (session cookie), storage, weather, per-user engine config
  csrf.py        session-bound CSRF middleware + dependency
  security.py    security headers, structured request logging
  schemas.py     ItemData validation, closet filters
  routes/        health, pages, auth, account, settings, closet, items, outfits, planner, candidates, media
  services/      auth, passwords, mailer, weather, preferences, feedback, planner, account, items, outfits, candidates, analysis, fetcher, scraper, imaging, tagging, pipeline, storage
  engine/        pure-Python outfit engine (vocab, rules, scoring, generator); see docs/
  templates/     Jinja2 + HTMX templates
  static/        style.css (tokens mirror DESIGN.md)
scripts/         seed_closet.py, create_user.py, demos (demo_account_flow.py, ...)
migrations/      Alembic
tests/           pytest; runs against TEST_DATABASE_URL (Postgres only)
```

## Creating a migration

1. Edit `app/models.py`.
2. `make revision m="add foo to items"` (autogenerates against the DB in `DATABASE_URL`;
   run `make migrate` first so it is up to date).
3. Review the file in `migrations/versions/`. Autogenerate misses things like enum type
   drops in `downgrade()`.
4. `make migrate`.
