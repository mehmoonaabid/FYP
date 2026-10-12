"""Portable DB layer: Postgres (Supabase) in production, SQLite for local tests.

Set DATABASE_URL to the Supabase *connection pooler* string, e.g.
  postgresql+psycopg://postgres.xxxx:PASSWORD@aws-0-xx.pooler.supabase.com:5432/postgres
Without it, a local SQLite file is used.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, datetime, timezone

from sqlalchemy import Date, bindparam, create_engine, text
from sqlalchemy.engine import Engine

SQLITE_DDL = """
create table if not exists raw_postings (
    id integer primary key autoincrement,
    source text not null, source_id text not null, url text not null,
    fetched_at text not null, content_hash text not null, html text not null,
    unique (source, source_id, content_hash)
);
create table if not exists postings (
    id integer primary key autoincrement,
    source text not null, source_id text not null, url text not null,
    title text, organization text, city text, posted_date text,
    apply_phone_hash text, apply_phone_masked text,
    image_url text, ocr_text text, ocr_engine text,
    deadline text, experience text, description text, skills text, work_mode text, salary text,
    first_seen_at text not null, last_seen_at text not null,
    unique (source, source_id)
);
create table if not exists scrape_runs (
    id integer primary key autoincrement,
    source text not null, started_at text not null, finished_at text,
    status text not null default 'running',
    discovered integer not null default 0, new_postings integer not null default 0,
    errors integer not null default 0, newest_posted text, note text
);
create table if not exists skipped_postings (
    id integer primary key autoincrement,
    source text not null, source_id text not null, reason text not null,
    posted_date text, skipped_at text not null,
    unique (source, source_id)
);
"""

# Columns written by upsert_posting; adapters may leave any of the optional ones out.
POSTING_COLS = (
    "source", "source_id", "url", "title", "organization", "city", "posted_date",
    "apply_phone_hash", "apply_phone_masked", "image_url", "ocr_text", "ocr_engine",
    "deadline", "experience", "description", "skills", "work_mode", "salary",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = os.environ.get("DATABASE_URL", "sqlite:///intellihire_local.db")
        _engine = create_engine(url, pool_pre_ping=True, future=True)
        if _engine.dialect.name == "sqlite":
            with _engine.begin() as conn:
                for stmt in SQLITE_DDL.split(";"):
                    if stmt.strip():
                        conn.execute(text(stmt))
    return _engine


def reset_engine() -> None:
    """For tests: drop the cached engine so DATABASE_URL is re-read."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


@contextmanager
def tx():
    with get_engine().begin() as conn:
        yield conn


def known_source_ids(source: str) -> set[str]:
    """IDs we never need to fetch again: stored postings plus ads already judged out-of-window."""
    with tx() as c:
        rows = c.execute(
            text("select source_id from postings where source=:s "
                 "union select source_id from skipped_postings where source=:s"),
            {"s": source},
        )
        return {r[0] for r in rows}


def record_skipped(source: str, source_id: str, reason: str, posted_date=None) -> None:
    with tx() as c:
        c.execute(
            text("insert into skipped_postings (source, source_id, reason, posted_date, skipped_at) "
                 "values (:s, :i, :r, :d, :t) on conflict do nothing"),
            {"s": source, "i": source_id, "r": reason, "d": posted_date, "t": now_iso()},
        )


def insert_raw(source: str, source_id: str, url: str, content_hash: str, html: str) -> None:
    with tx() as c:
        c.execute(
            text(
                "insert into raw_postings (source, source_id, url, fetched_at, content_hash, html) "
                "values (:s, :i, :u, :f, :h, :html) on conflict do nothing"
            ),
            {"s": source, "i": source_id, "u": url, "f": now_iso(), "h": content_hash, "html": html},
        )


def upsert_posting(p: dict) -> bool:
    """Insert or refresh a posting. Returns True if it was new. Missing optional keys become NULL."""
    row = {k: p.get(k) for k in POSTING_COLS}
    cols = ", ".join(POSTING_COLS)
    vals = ", ".join(":" + k for k in POSTING_COLS)
    with tx() as c:
        existed = c.execute(
            text("select 1 from postings where source=:s and source_id=:i"),
            {"s": row["source"], "i": row["source_id"]},
        ).first()
        ts = now_iso()
        c.execute(
            text(
                f"""
                insert into postings ({cols}, first_seen_at, last_seen_at)
                values ({vals}, :ts, :ts)
                on conflict (source, source_id) do update set
                    title=excluded.title, organization=excluded.organization, city=excluded.city,
                    posted_date=excluded.posted_date, image_url=excluded.image_url,
                    deadline=excluded.deadline, experience=excluded.experience,
                    description=excluded.description, skills=excluded.skills,
                    work_mode=excluded.work_mode, salary=excluded.salary,
                    last_seen_at=excluded.last_seen_at
                """
            ),
            {**row, "ts": ts},
        )
        return existed is None


def start_run(source: str) -> int:
    with tx() as c:
        return int(c.execute(
            text("insert into scrape_runs (source, started_at, status) "
                 "values (:s, :t, 'running') returning id"),
            {"s": source, "t": now_iso()},
        ).scalar())


def finish_run(run_id: int, status: str, discovered=0, new=0, errors=0, newest=None, note=None) -> None:
    with tx() as c:
        c.execute(
            text(
                "update scrape_runs set finished_at=:f, status=:st, discovered=:d, new_postings=:n, "
                "errors=:e, newest_posted=:np, note=:note where id=:id"
            ),
            {"f": now_iso(), "st": status, "d": discovered, "n": new, "e": errors,
             "np": newest, "note": note, "id": run_id},
        )


def run_in_progress(source: str, stale_minutes: int = 90) -> bool:
    """Cheap lock: a 'running' row younger than stale_minutes means another run is active.
    90 minutes so a long first backfill is not mistaken for a dead run."""
    with tx() as c:
        rows = c.execute(
            text("select started_at from scrape_runs where source=:s and status='running' "
                 "order by id desc limit 1"),
            {"s": source},
        ).first()
    if not rows:
        return False
    started = rows[0]
    if isinstance(started, str):
        started = datetime.fromisoformat(started)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds() < stale_minutes * 60


def max_posted_date(source: str):
    with tx() as c:
        return c.execute(
            text("select max(posted_date) from postings where source=:s"), {"s": source}
        ).scalar()


def list_postings(city=None, q=None, limit=50, offset=0, since=None, source=None, today=None) -> list[dict]:
    """since: only postings with posted_date >= since whose deadline (if any) has not passed.
    since=None means no age/deadline filter, i.e. include the archive."""
    sql = ("select source, source_id, url, title, organization, city, posted_date, deadline, "
           "experience, skills, work_mode, salary, substr(description, 1, 500) as description, "
           "apply_phone_masked, image_url, ocr_text from postings where 1=1")
    params: dict = {"limit": min(limit, 200), "offset": offset}
    if source:
        sql += " and source=:source"
        params["source"] = source
    if since is not None:
        sql += " and posted_date >= :since and (deadline is null or deadline >= :today)"
        params["since"] = since
        params["today"] = today or date.today()
    if city:
        sql += " and lower(city)=lower(:city)"
        params["city"] = city
    if q:
        sql += (" and (lower(coalesce(title,'')) like :q or lower(coalesce(ocr_text,'')) like :q"
                " or lower(coalesce(skills,'')) like :q)")
        params["q"] = f"%{q.lower()}%"
    sql += " order by posted_date desc, source_id desc limit :limit offset :offset"
    stmt = text(sql)
    if since is not None:
        stmt = stmt.bindparams(bindparam("since", type_=Date), bindparam("today", type_=Date))
    with tx() as c:
        return [dict(r._mapping) for r in c.execute(stmt, params)]


def last_run(source: str) -> dict | None:
    with tx() as c:
        r = c.execute(
            text("select * from scrape_runs where source=:s order by id desc limit 1"), {"s": source}
        ).first()
    return dict(r._mapping) if r else None
