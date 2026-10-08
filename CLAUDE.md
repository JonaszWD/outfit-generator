# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# OutfitGenerator

"Wardrobe": FastAPI + Jinja2/HTMX app. Users upload clothing photos (background removed, AI-suggested tags, user confirms), get scored outfit suggestions, and paste a product link to see how many new outfits it would unlock. See README.md for per-phase feature detail and settings tables.

## Commands

Python 3.12, `uv`, Docker (Postgres 16), `make`. Copy `.env.example` to `.env` first.

```bash
make install        # uv sync --extra cutout (rembg is an optional extra)
make up             # Postgres (creates wardrobe + wardrobe_test DBs on fresh volume)
make migrate        # alembic upgrade head
make dev            # uvicorn --factory --reload on :8000 (health: /health)
make test           # pytest, skips -m slow
make lint           # ruff check + ruff format --check
make format         # ruff --fix + format
make typecheck      # mypy (strict; covers app, tests, migrations)
make revision m="msg"   # alembic autogenerate
```

- Single test: `uv run pytest tests/test_closet_routes.py::test_name -x`. Slow tests (real rembg model, engine benchmark): `uv run pytest -m slow -s`.
- Tests need Postgres up and `TEST_DATABASE_URL` set; `tests/conftest.py` runs alembic against that DB.
- Dev helpers: `make user EMAIL=...`, `make seed EMAIL=...`, `make demo`, `make demo-account`, `make demo-link` (scripts in `scripts/`).
- `make docker` runs app + db in containers with live reload; `make docker-rebuild` only after changing pyproject/uv.lock/Dockerfile.
- Ruff line length 100; rules E,F,I,UP,B,SIM. Mypy strict.

## Architecture

- `app/main.py` `create_app()` factory. Routers in `PUBLIC_ROUTERS` (health, pages, auth) are open; **every other router is registered with the `get_current_user` dependency**, so new routers are private by default. `tests/test_route_auth.py` walks all routes and fails if an unlisted one answers without a session.
- Layers: `app/routes/` (thin HTTP, HTMX partials via `_*.html` templates) -> `app/services/` (all logic: pipeline, tagging, scraping, outfits, planner, auth, storage, weather) -> `app/models.py` (SQLModel) / `app/db.py`. Dependencies (storage, tagger, mailer, weather, pipeline/candidate deps) are injected through `app/deps.py` so tests override them with fakes (`FakeTagger`, `FakeWeatherProvider`, `LocalDiskStorage` on tmp dir).
- **`app/engine/` is pure Python: no DB/web/ORM imports (a test enforces it).** Services convert DB rows to `ItemView` at the edge (`app/services/outfits.py`) and call the engine with an `EngineConfig`. Hard rules + sanity table decide validity; soft scoring (weighted geometric mean, `min_score` 0.6) ranks. Tunables live in `app/engine/config.py` / `scoring.py`; a test checks `docs/outfit-engine.md` tables match the defaults, so update both together. Allowed tag values: `app/engine/vocab.py` (drives forms, validation, filters).
- Upload pipeline (`app/services/pipeline.py`, `process_item(item_id)`): `pending -> processing -> done|failed`, run as background task. Takes only an id and builds its own session/deps so it can move to a queue unchanged. AI output stays in `ai_tags`/`ai_confidence`; item columns hold only confirmed tags; only `tags_confirmed` items feed the engine.
- Candidates (link analysis) are `Item` rows with `source=candidate`. Every closet/stats/generation/review query must filter `source=owned`. All user-supplied URL fetching must go through `app/services/fetcher.py` (SSRF guards: public IPs only, pinned IP, redirect re-check, size limits); scraper respects robots.txt.
- Every item/outfit query is scoped to the current user; other users' rows return 404. Media is served via an auth-checked route (`/media/<user_id>/<file>`), never a static mount; code talks to the `Storage` protocol only.
- Security middleware order matters (`create_app`): security headers outermost, then request logging, then CSRF. CSP forbids inline scripts: JS goes in `app/static/app.js`. HTMX sends CSRF via `X-CSRF-Token` from `hx-headers` on `<body>`; forms use a hidden field. Sessions are server-side in Postgres (token stored hashed). Log only exception types / hosts, never user data or image bytes.
- Config via pydantic-settings (`app/config.py`); app refuses to start with default `SECRET_KEY` when `ENV=production`. `TAGGER_BACKEND=fake` (default) keeps everything offline; `claude` sends EXIF-stripped photos to the Anthropic API.
- Style references (`app/engine/style.py`, `app/services/style.py`): reference photos are read once into a vocab-validated `StyleResult` (backend `STYLE_BACKEND`: fake/claude/ollama), merged into a `StyleProfile`, and passed to `generate_outfits(style=...)` as a capped re-rank like personalization. Off by default per request (`style_ai` form field); `style=None` must stay identical to the plain engine.
- Hosting: runs in Docker (disk, rembg, background tasks) or on Vercel (`api/index.py`, `vercel.json`, `requirements.txt` from `make vercel-requirements`). Serverless switches: `STORAGE_BACKEND` (local/supabase/s3, always served via the auth-checked `/media` route), `CUTOUT_BACKEND=none`, `PROCESS_IN_REQUEST` (items tag via `POST /items/{id}/process`, jobs via `run_or_schedule`), `DB_SERVERLESS`, `TRUST_PROXY_HEADERS`. Keep rembg out of anything imported at startup; CI runs without it. Registration is closed in production unless `INVITE_CODE`/`REGISTRATION_OPEN`.
- Docs: `docs/deployment.md`, `docs/outfit-engine.md`, `docs/style-references.md`, `docs/link-analysis.md`, `docs/accessibility.md`, `PRODUCT.md`.

## Design system (mandatory)

All UI work (HTML, CSS, templates, JS/TS components, SVG, any visual styling) MUST follow the design system below. No exceptions, no "close enough".

@DESIGN.md

Rules:
- Use only tokens from DESIGN.md (colors, typography, spacing, radii, components). Never invent hex values, fonts, sizes, or radii. Define tokens once as CSS variables mirroring DESIGN.md and reference them.
- Before writing or editing any UI file, re-read the relevant DESIGN.md section (components, Do/Don't).
- Respect every "Don't" in DESIGN.md.
- If DESIGN.md lacks a token/component you need, stop and ask the user. Do not improvise.
- After UI changes, list which DESIGN.md tokens/components were used and confirm no deviations.

## Git (mandatory)

NEVER run `git commit`, `git push`, or GitHub write commands (`gh pr create/merge`, etc.). A hook blocks them. When work is done, tell the user and let them commit.
