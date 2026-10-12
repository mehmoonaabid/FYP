# IntelliHire MVP: Ingestion Pipeline & API..

Automated data ingestion and processing pipeline for centralizing, deduplicating, and screening fragmented job opportunities across Pakistani employment portals and classifieds.

---

## Architecture Overview

```text
  [Job Portals & Classifieds]
              │
              ▼
  [Python Ingestion Layer] (Requests + BeautifulSoup4 / Playwright)
              │
              ▼
  [Schema Normalization & Entity Hash Engine]
              │
              ▼
  [Supabase PostgreSQL] (Session Pooler via psycopg3)
              │
              ▼
  [FastAPI Service] (REST Endpoints & Scheduled Ingestion Cron Triggers)
```

---

## Prerequisites

- **Python 3.10+** installed
- A **Supabase** project instance
- Windows PowerShell / Linux Terminal

---

## 1. Local Environment Setup

Clone or extract the project to your local directory (e.g., `D:\FullStack\fyp_project\intellihire`) and open a terminal inside the root folder:

```powershell
# 1. Allow script execution (Windows only, run once)
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

# 2. Create the virtual environment
python -m venv .venv

# 3. Activate the virtual environment
.\.venv\Scripts\Activate.ps1

# 4. Install dependencies
pip install -r requirements.txt

# 5. Verify installation
python -c "import sqlalchemy, fastapi, bs4; print('ok')"
```

---

## 2. Database Provisioning (Supabase)

1. Open your **Supabase Dashboard** and navigate to **SQL Editor**.
2. Run the base schema from `supabase/schema.sql`.
3. Run any subsequent migration scripts (e.g., `supabase/migration_002_rozee.sql`).
4. Navigate to **Table Editor** to confirm the `postings` and related staging tables are present.

---

## 3. Environment Variables Configuration

Set required runtime variables in your active terminal.

> **Important (URL Encoding):** When using passwords with special characters (like `@`, `#`, `&`), encode them before placing them in the `DATABASE_URL` (e.g., `@` becomes `%40`, `&` becomes `%26`).

```powershell
$env:DATABASE_URL="postgresql+psycopg://postgres.<PROJECT_REF>:<ENCODED_PASSWORD>@<POOLER_HOST>:5432/postgres"
$env:PHONE_SALT="k9X2mQ8vL4tP1wZ7yR3bN6cE5jH0uA9x"
$env:CRON_SECRET="s7D4fG9hJ2kL5zX8cV1bN3mQ6wE0rT4y"
$env:SCRAPER_USER_AGENT="IntelliHire-FYP/0.1 (academic research; contact: your_email@domain.com)"
```

---

## 4. Running the Pipeline

### Manual Single-Source Test Run
To verify scraper connectivity and test insertion into Supabase:

```powershell
python -c "from intellihire import pipeline; print(pipeline.run_jang())"
```

Verify newly inserted records in the Supabase **Table Editor -> postings** table.

---

## 5. Starting the FastAPI Service

In the terminal where your environment variables are configured, start the web server:

```powershell
uvicorn api.main:app --reload
```

The API will be accessible at `http://127.0.0.1:8000`.

### Key Endpoints:
- **Swagger Documentation:** `http://127.0.0.1:8000/docs`
- **Health Check:** `http://127.0.0.1:8000/api/health`
- **View Postings:** `http://127.0.0.1:8000/api/postings`

---

## 6. Triggering Scheduled Scrapes

In a separate terminal, trigger an asynchronous background crawl using the configured Bearer token:

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/cron/scrape -H "Authorization: Bearer s7D4fG9hJ2kL5zX8cV1bN3mQ6wE0rT4y"
```

Expected response:
```json
{"accepted": true}
```

---

## Project Roadmap

- [x] Phase 1: MVP Scraper Ingestion & Supabase Staging Integration
- [x] Phase 1: Protected FastAPI Cron Controller
- [ ] Phase 2: Rozee Sitemap URL Discovery & Incremental Crawler
- [ ] Phase 2: Text Normalization and Contact Anonymization Engine
- [ ] Phase 3: Semantic Deduplication Pipeline via Sentence-Transformers (`all-MiniLM-L6-v2`)
- [ ] Phase 4: Multi-Factor Fraud & Anomaly Scoring Classifier