# Deploying to Vercel (free tiers)

Wardrobe runs as one Vercel function (`api/index.py`) with Postgres and photo storage in
Supabase and tagging by Gemini. Docker (`docker compose`) still works for development and for a
normal server; nothing here changes that.

> **Not yet tried on a live Vercel project.** The configuration was built from Vercel's and Supabase's
> public documentation and tested against mocks. Expect to adjust something on
> the first deploy, and read the build log. Plans and free-tier limits change: check them.

## What differs on Vercel

| Docker / laptop | Vercel |
|---|---|
| Photos in `media/` on disk | A private Supabase Storage bucket (`STORAGE_BACKEND=supabase`), always served through the login-checked `/media` route |
| Transparent cutouts with rembg | None: the original photo is shown (`CUTOUT_BACKEND=none`). rembg and its model are far over the ~250 MB function limit |
| Tagging in a background task | Each new photo's card asks the server to tag it, one at a time (`PROCESS_IN_REQUEST=true`) |
| Photos up to `MAX_UPLOAD_MB` | The browser shrinks photos to 1600 px before sending, because Vercel rejects request bodies over about 4.5 MB (`app/static/app.js`) |
| A connection pool | A fresh connection per request through the provider's pooler (`DB_SERVERLESS=true`) |
| Anyone can register in development | Closed in production unless `INVITE_CODE` is set (or `REGISTRATION_OPEN=true`) |

Bundle size: the Linux install from `requirements.txt` is about 214 MB, under the 250 MB limit
with little room to spare. Think twice before adding a heavy dependency.

## One-time setup

1. **Supabase** (free project)
   - Create the project and remember the database password.
   - Storage: create a bucket named `wardrobe` and leave it **private**.
   - Settings, API: copy the project URL and the `service_role` key (keep it secret: it ignores
     all access rules, so it only ever goes in server environment variables).
   - Settings, Database: copy the *connection pooling* string (transaction mode, port 6543) for
     the app, and the *session* or direct string for migrations. Add `?sslmode=require` if absent.
     Use the `postgresql+psycopg://` scheme.
2. **Create the tables** from your laptop, against the session or direct string:

   ```bash
   DATABASE_URL='postgresql+psycopg://...' uv run alembic upgrade head
   ```
   Run it again after every release that adds a file under `migrations/versions/`.
3. **Gemini:** an API key from AI Studio. `gemini-3.5-flash-lite` has the generous limits
   (about 15 requests a minute, 500 a day on the free tier at the time of writing).
4. **Vercel:** import the Git repository (or run `vercel` in this folder). Set the environment
   variables below for *Production*. Vercel reads `requirements.txt`; the build log should show
   no `rembg`.
5. **First account:** with `INVITE_CODE` set, register at `/register` using the code. Or create
   one from your laptop: `DATABASE_URL='...' make user EMAIL=you@example.com`.

## Environment variables

```
ENV=production
SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(48))">
APP_BASE_URL=https://<your-project>.vercel.app
DATABASE_URL=<pooled string>
DB_SERVERLESS=true
TRUST_PROXY_HEADERS=true

STORAGE_BACKEND=supabase
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_KEY=<service_role key>
SUPABASE_BUCKET=wardrobe

CUTOUT_BACKEND=none
PROCESS_IN_REQUEST=true
MAX_UPLOAD_MB=4
MAX_BATCH_FILES=6
AI_TIMEOUT_SECONDS=40

TAGGER_BACKEND=gemini
STYLE_BACKEND=gemini
GEMINI_API_KEY=<key>
GEMINI_MODEL=gemini-3.5-flash-lite

INVITE_CODE=<something your friends can type>
EMAIL_BACKEND=smtp          # SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM
```

The app refuses to start in production on a Vercel function with local photo storage or rembg,
and logs a warning for a fake tagger, console email, a localhost `APP_BASE_URL` and the two
serverless switches. Without working email, password-reset links only appear in the logs.

## After deploying: check these

- `/health` returns `{"status": "ok", "database": "up"}`.
- Register, log in, add three photos: each card shows "Tagging…" and then its tags, one after
  another.
- Open a photo URL (`/media/<id>/<file>`) in a private window: it must answer with the login page,
  never the image. In Supabase, open the bucket's public URL for a file: it must be refused.
- Reload `/closet`, then `/outfits`, and generate outfits.

## Limits and sharp edges

- **Function time:** `vercel.json` asks for 60 seconds, the Hobby maximum for a normal function at
  the time of writing. A tagging call is a few seconds, but Gemini sometimes takes much longer;
  `AI_TIMEOUT_SECONDS=40` makes a stuck call fail with a Retry button instead of a dead function.
- **Gemini quota:** retries on 429 and 5xx wait for the server's `Retry-After` (at most 20 s).
  Tagging one photo at a time keeps a normal upload under the per-minute limit; a batch of 30
  or more may need a pause. Style reference reading shares the same quota.
- **Per-instance memory:** the robots.txt cache, the host politeness delay and the weather cache
  live in each function instance. Logins and reset requests are rate limited in Postgres, so
  those are fine; link analysis may revisit a robots.txt more often than on a single server.
- **Free tiers pause or cap.** A free Supabase project may pause after a period of inactivity
  and has small storage and database limits; check both. Vercel Hobby is for non-commercial use.
- **Backups:** the free plans may not include backups. Take your own: `pg_dump` the database and
  download the bucket now and then.
- **Leaving Vercel:** the same code runs in Docker. Set `STORAGE_BACKEND=s3` for any
  S3-compatible bucket (Cloudflare R2, Backblaze B2, AWS S3) instead of Supabase.

## Keeping it healthy

- CI (`.github/workflows/ci.yml`) runs ruff, mypy and the tests *without* rembg, which is the
  dependency set Vercel installs, and fails if `requirements.txt` is out of date.
- After changing dependencies: `uv lock`, then `make vercel-requirements`.
- Changing `SECRET_KEY` logs everyone out and invalidates reset links.
