# Wardrobe

Website where you upload photos of your clothes, get outfit suggestions, and paste a product
link to see how many new outfits that piece would unlock.

**Status: Phase 0 (foundation).** Schema, health check and tooling only. No outfit logic,
tagging, scraping, auth or uploads yet.

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
make dev                 # http://localhost:8000, health at /health
```

If port 5432 is already used on your machine, set `DB_HOST_PORT` (e.g. `5433`) in `.env` and
use the same port in `DATABASE_URL` and `TEST_DATABASE_URL`.

The test database is created only on first start of a fresh volume. If you already have a
volume, run `docker compose exec db sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB -c "CREATE DATABASE wardrobe_test"'`
or `docker compose down -v` (this deletes data).

Run everything in containers instead: `docker compose up --build` (runs migrations, then
Uvicorn with reload on port 8000).

## Make targets

`install`, `up`, `down`, `migrate`, `revision m="message"`, `test`, `lint`, `format`,
`typecheck`, `dev`.

## Layout

```
app/
  main.py        app factory, router registration
  config.py      settings from environment (pydantic-settings)
  db.py          engine, session dependency
  models.py      SQLModel tables: users, items, outfits, candidates
  routes/        health.py, pages.py
  services/      business logic (empty)
  engine/        outfit engine (empty)
  templates/     Jinja2 templates
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
