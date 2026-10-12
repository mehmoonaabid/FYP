# IntelliHire MVP: job ingestion (Rozee.pk + Jang)

Scrapes job sources incrementally, stores raw HTML first, parses fields, keeps only **active** jobs,
and serves them over a small API. A cron endpoint lets a scheduler (Upstash QStash) keep it fresh.

| Source | What it is | Status |
|---|---|---|
| `rozee` | Rozee.pk, text jobs with deadlines, discovered through its sitemap | live data |
| `jang` | Jang newspaper ads (image only) | its /jobs section stopped updating in Oct 2023: kept as a dev/OCR dataset |

## Layout
```
supabase/schema.sql              full schema for a NEW project
supabase/migration_002_rozee.sql run this on an EXISTING project (adds columns + skipped_postings)
intellihire/common.py            shared delay, user agent, date parsing
intellihire/freshness.py         what counts as an active job
intellihire/rozee.py             Rozee adapter: sitemap discovery, redirect-safe fetch, parser
intellihire/jang.py              Jang adapter
intellihire/pipeline.py          one run for any source: discover -> fetch -> parse -> freshness gate -> store
intellihire/db.py                Postgres (Supabase) in prod, SQLite locally
intellihire/cli.py               `run` and `probe` commands
api/main.py                      GET /api/postings, GET /api/health, POST /api/cron/scrape[/{source}]
tests/test_mvp.py                parsers, freshness, pipeline, API auth
```

## Run (from the folder that contains requirements.txt)
```powershell
pip install -r requirements.txt
$env:DATABASE_URL="postgresql+psycopg://postgres.xxxx:PASSWORD@aws-0-xx.pooler.supabase.com:5432/postgres"
$env:PHONE_SALT="any-long-random-string"
$env:SCRAPER_USER_AGENT="IntelliHire-FYP/0.1 (academic research; contact: you@university.edu.pk)"

python -m intellihire.cli probe rozee          # 1. fetch ONE page, save its HTML, show parsed fields
$env:MAX_NEW_PER_RUN="10"
python -m intellihire.cli run rozee            # 2. small test run
$env:MAX_NEW_PER_RUN="500"
python -m intellihire.cli run rozee            # 3. full first backfill (~25-40 min), later runs only fetch new jobs
pytest                                         # unit tests (they use a temp SQLite file, not Supabase)
uvicorn api.main:app --reload                  # API on http://127.0.0.1:8000/docs
```
`probe` prints `EMPTY FIELDS: ...` if the parser missed something. Send the saved file from `probe_out/`
and the parser can be fixed in one pass.

## Environment variables
| Name | Purpose |
|---|---|
| `DATABASE_URL` | Supabase pooler string, `postgresql+psycopg://...` (omit locally) |
| `PHONE_SALT` | Secret salt for phone hashing (set once, never change) |
| `SCRAPER_USER_AGENT` | Must contain a real contact email |
| `PUBLIC_BASE_URL` | Public URL of the deployed API, no trailing slash (used to verify QStash) |
| `QSTASH_CURRENT_SIGNING_KEY`, `QSTASH_NEXT_SIGNING_KEY` | From the Upstash console |
| `CRON_SECRET` | Bearer secret for manual or GitHub Actions triggers |
| `MAX_AGE_DAYS` | Active window in days (default 60; use 30 for one month) |
| `REQUIRE_CURRENT_YEAR` | `1` to also require the posting year to equal this year (default off) |
| `MAX_NEW_PER_RUN` | New ads fetched per run (default 40) |
| `STALE_STREAK_STOP` | Jang only: stop after this many consecutive out-of-window ads (default 5) |
| `OCR_ENABLED`, `OCR_LANGS` | `1` to enable OCR for image ads (Jang); default `eng+urd` |

## Freshness: what counts as an active job
The source's own "open" status is not trusted. A posting is active only if **all** hold:
1. it has a posting date, not in the future, within `MAX_AGE_DAYS` of today
2. if the source gives an apply-by deadline, it has not passed
3. (optional) `REQUIRE_CURRENT_YEAR=1`: posted this calendar year

Applied twice:
- **At ingest:** failing ads are not stored as postings. They go in `skipped_postings` (reason + date, no HTML),
  so they are never fetched again.
- **At read:** `GET /api/postings` applies the same rules, so postings age out or expire by themselves.
  `?max_age_days=30` narrows the window; `?include_archive=true` (dev only) shows everything; `?source=rozee` filters.

If a Rozee page has no readable posted date, the sitemap's `lastmod` is used instead (noted in the run log
as `posted_date_from_sitemap`). A page with no readable title is **not stored** and counts as an error
(`PARSER_EMPTY`), so a layout change or block page cannot fill the database with empty rows.

## Automation
1. Deploy the API on a persistent host (Render, Railway, Fly, or a HF Space), not a serverless function: the scrape
   runs as a background task after the 202 response.
2. Schedule it with QStash. Use the **path** form so the signature covers the exact URL (check Upstash docs for syntax):
   ```bash
   curl -X POST "https://qstash.upstash.io/v2/schedules/https://YOUR-API/api/cron/scrape" \
     -H "Authorization: Bearer $QSTASH_TOKEN" -H "Upstash-Cron: 0 */6 * * *"
   ```
   `/api/cron/scrape` runs every source; `/api/cron/scrape/rozee` runs one.
3. Safety built in: signed requests, a lock per source, a per-run cap, and a 429/503 stops the run at once.
4. Monitor `GET /api/health`: each source reports its last run and whether it is `source_stale`.
5. Zero-infrastructure alternative: `.github/workflows/scrape.yml` (set `DATABASE_URL` and `PHONE_SALT` as secrets).
6. Redis is not needed yet. Add it later for caching or a worker queue.

## Responsible use
Rozee's terms allow educational use but not jobs/API abuse. So: 2-4 s between requests, a user agent with a real
contact, only allow-listed hosts (`rozee.pk`, `rozeegpt.ai`) are ever fetched even if a redirect points elsewhere,
e-mails and phone numbers are stripped from stored text, and listings should not be republished publicly: link back to
the source instead. Consider emailing Rozee about the FYP.

## Known limits
- **Rozee parsing is unverified against raw HTML.** It was written from a summarising fetch tool: JSON-LD first, then label
  matching. Run `probe` first.
- Rozee job URLs redirect to `rozeegpt.ai`; the client follows that by hand with a host allow-list.
- Rozee is a vetted portal, so it has almost no scams: good for extraction and dedup work, not fraud training.
- Jang ads are images; the organization is usually the generic "Private Company". Real details need OCR.
- If you ran an older version with a local SQLite file, delete `intellihire_local.db` (its tables lack the new columns).


## ENV 
$env:DATABASE_URL="postgresql+psycopg://postgres.yarsbrdbohllhnenjtou:TQfwFv9QU%40%26.d%40P@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres"
$env:PHONE_SALT="k9X2mQ8vL4tP1wZ7yR3bN6cE5jH0uA9x"
$env:CRON_SECRET="s7D4fG9hJ2kL5zX8cV1bN3mQ6wE0rT4y"
$env:SCRAPER_USER_AGENT="IntelliHire-FYP/0.1 (academic research; contact: your_email@example.com)"