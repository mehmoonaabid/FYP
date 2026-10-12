"""FastAPI service: public read API + a cron endpoint triggered by Upstash QStash.

Run locally:  uvicorn api.main:app --reload
Deploy on a persistent host (Render / Railway / Fly / HF Space). Do NOT use a serverless
function: the scrape runs as a background task and would be killed when the response ends.

Cron URLs (point QStash at one of these):
  POST /api/cron/scrape            run every source
  POST /api/cron/scrape/rozee      run one source (path form: the QStash signature covers this exact URL)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os

import jwt  # PyJWT
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request

from intellihire import db, freshness, pipeline

log = logging.getLogger("intellihire.api")
app = FastAPI(title="IntelliHire API", version="0.3.0")

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")  # e.g. https://intellihire.onrender.com


def verify_qstash(signature: str | None, body: bytes, url: str) -> bool:
    """Verify Upstash-Signature (JWT, HS256) against current and next signing keys."""
    if not signature:
        return False
    for key in (os.environ.get("QSTASH_CURRENT_SIGNING_KEY"), os.environ.get("QSTASH_NEXT_SIGNING_KEY")):
        if not key:
            continue
        try:
            claims = jwt.decode(
                signature, key, algorithms=["HS256"], issuer="Upstash",
                options={"require": ["exp", "nbf", "sub", "body"]}, leeway=5,
            )
        except jwt.PyJWTError:
            continue
        expected = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("=")
        if claims["sub"] == url and claims["body"].rstrip("=") == expected:
            return True
    return False


def _authorized(request: Request, body: bytes, signature: str | None, authorization: str | None) -> bool:
    # 1) QStash signed request (production path). The signed URL is the exact URL QStash called.
    signed_url = PUBLIC_BASE_URL + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if verify_qstash(signature, body, signed_url):
        return True
    # 2) Shared secret, for manual runs / GitHub Actions fallback
    secret = os.environ.get("CRON_SECRET")
    if secret and authorization and hmac.compare_digest(authorization, f"Bearer {secret}"):
        return True
    return False


def _run_safely(source: str | None) -> None:
    try:
        result = pipeline.run_source(source) if source else pipeline.run_all()
        log.info("scrape finished: %s", result)
    except Exception:
        log.exception("scrape crashed")


@app.post("/api/cron/scrape", status_code=202)
@app.post("/api/cron/scrape/{source}", status_code=202)
async def cron_scrape(
    request: Request,
    background: BackgroundTasks,
    source: str | None = None,
    upstash_signature: str | None = Header(default=None, alias="Upstash-Signature"),
    authorization: str | None = Header(default=None),
):
    body = await request.body()
    if not _authorized(request, body, upstash_signature, authorization):
        raise HTTPException(status_code=401, detail="unauthorized")
    if source is not None and source not in pipeline.SOURCES:
        raise HTTPException(status_code=404, detail=f"unknown source; choose from {sorted(pipeline.SOURCES)}")
    background.add_task(_run_safely, source)  # answer immediately; QStash expects a fast response
    return {"accepted": True, "source": source or "all"}


@app.get("/api/postings")
def postings(
    city: str | None = None,
    q: str | None = Query(default=None, description="search in title / skills / OCR text"),
    source: str | None = Query(default=None, description="e.g. rozee or jang"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    max_age_days: int | None = Query(default=None, ge=1, le=3650,
                                     description="override the default active window (MAX_AGE_DAYS)"),
    include_archive: bool = Query(default=False, description="dev only: ignore the age and deadline filters"),
):
    since = None if include_archive else freshness.cutoff_date(days=max_age_days)
    rows = db.list_postings(city=city, q=q, limit=limit, offset=offset, since=since, source=source)
    return {"count": len(rows), "active_since": str(since) if since else None, "results": rows}


@app.get("/api/health")
def health(source: str | None = None):
    names = [source] if source else list(pipeline.SOURCES)
    out = {}
    for name in names:
        if name not in pipeline.SOURCES:
            raise HTTPException(status_code=404, detail="unknown source")
        run = db.last_run(name)
        newest = db.max_posted_date(name)
        stale = bool(run and run.get("note") and "SOURCE_STALE" in run["note"])
        out[name] = {"last_run": run, "newest_posting_date": str(newest) if newest else None,
                     "source_stale": stale}
    return {"sources": out}
