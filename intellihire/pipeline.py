"""One incremental scrape run, for any registered source:

    discover -> fetch new -> parse -> FRESHNESS GATE -> raw layer -> (OCR) -> upsert

Designed to be called from a cron trigger (QStash / GitHub Actions) and to be safe to call twice:
a lock row prevents overlap per source and everything is idempotent on (source, source_id).

Freshness gate: only ads posted within MAX_AGE_DAYS (default 60), and not past their apply-by
deadline, become postings. Others are recorded in skipped_postings (so they are never fetched
again) and their HTML is not stored. For sources whose ids grow with time (client.early_stop
is True) a run stops after STALE_STREAK_STOP consecutive skipped ads.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Callable

from . import common, db, freshness, jang, rozee
from .ocr import ocr_enabled, run_ocr

log = logging.getLogger("intellihire")

MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "40"))
STALE_DAYS = int(os.environ.get("STALE_DAYS", "14"))
STALE_STREAK_STOP = int(os.environ.get("STALE_STREAK_STOP", "5"))


@dataclass(frozen=True)
class SourceSpec:
    name: str
    client_factory: Callable[[], object]
    parse: Callable[[str, str, str], dict]   # (html, source_id, url) -> posting dict


SOURCES: dict[str, SourceSpec] = {
    jang.SOURCE: SourceSpec(jang.SOURCE, jang.JangClient, jang.parse_detail),
    rozee.SOURCE: SourceSpec(rozee.SOURCE, rozee.RozeeClient, rozee.parse_detail),
}


def run_source(source: str, client=None) -> dict:
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}; choose from {sorted(SOURCES)}")
    spec = SOURCES[source]

    if db.run_in_progress(source):
        run_id = db.start_run(source)
        db.finish_run(run_id, "locked", note="another run is in progress; skipped")
        return {"status": "locked"}

    client = client or spec.client_factory()
    early_stop = getattr(client, "early_stop", True)
    run_id = db.start_run(source)
    discovered = new = errors = empty_titles = from_sitemap = 0
    skipped: dict[str, int] = {}
    note_parts: list[str] = []
    try:
        listings = client.discover()
        discovered = len(listings)
        known = db.known_source_ids(source)
        todo = [l for l in listings if l.source_id not in known][:MAX_NEW_PER_RUN]

        stale_streak = 0
        for l in todo:
            try:
                html = client.fetch_detail(l)
                posting = spec.parse(html, l.source_id, l.url)
                if not posting.get("title"):
                    # Parser found nothing: layout changed, JS shell or block page. Do not store junk and
                    # do not remember the id, so it is retried once the parser is fixed.
                    empty_titles += 1
                    errors += 1
                    common.polite_sleep()
                    continue

                # If the page has no readable date, fall back to the sitemap's lastmod rather than
                # discarding every ad because of a parser miss.
                if posting.get("posted_date") is None and getattr(l, "lastmod", None):
                    posting["posted_date"] = l.lastmod
                    from_sitemap += 1

                reason = freshness.freshness_reason(posting["posted_date"], deadline=posting.get("deadline"))
                if reason:
                    db.record_skipped(source, l.source_id, reason, posting["posted_date"])
                    skipped[reason] = skipped.get(reason, 0) + 1
                    stale_streak += 1
                    if early_stop and stale_streak >= STALE_STREAK_STOP:
                        note_parts.append(
                            f"ARCHIVE_REACHED: stopped after {stale_streak} consecutive ads outside "
                            f"the {freshness.max_age_days()}-day window"
                        )
                        break
                    common.polite_sleep()
                    continue
                stale_streak = 0

                db.insert_raw(source, l.source_id, l.url, common.content_hash(html), html)
                if ocr_enabled() and posting.get("image_url") and hasattr(client, "download_image"):
                    try:
                        posting["ocr_text"], posting["ocr_engine"] = run_ocr(
                            client.download_image(posting["image_url"])
                        )
                    except Exception as e:  # OCR must never block ingestion
                        log.warning("OCR failed for %s: %s", l.source_id, e)
                        note_parts.append(f"ocr_failed:{l.source_id}")
                if db.upsert_posting(posting):
                    new += 1
            except RuntimeError as e:  # rate-limited: stop the whole run, don't hammer
                note_parts.append(str(e))
                errors += 1
                break
            except Exception as e:
                log.exception("failed on %s", l.source_id)
                errors += 1
                note_parts.append(f"error:{l.source_id}:{type(e).__name__}")
            common.polite_sleep()

        if skipped:
            note_parts.append("skipped=" + ",".join(f"{k}:{v}" for k, v in sorted(skipped.items())))
        if from_sitemap:
            note_parts.append(f"posted_date_from_sitemap={from_sitemap}")
        if empty_titles:
            note_parts.append(f"PARSER_EMPTY: {empty_titles} page(s) had no readable title; "
                              f"run `python -m intellihire.cli probe {source}` and check the saved HTML")

        # Staleness is judged on postings inside the active window only.
        newest = db.max_posted_date(source)
        if newest:
            nd = newest if isinstance(newest, date) else date.fromisoformat(str(newest)[:10])
            age = (date.today() - nd).days
            if age > STALE_DAYS:
                note_parts.append(f"SOURCE_STALE: newest posting is {age} days old")
        elif not new:
            note_parts.append("SOURCE_STALE: no postings stored")

        status = "ok" if errors == 0 else "error"
        db.finish_run(run_id, status, discovered, new, errors,
                      str(newest) if newest else None, "; ".join(note_parts) or None)
        return {"status": status, "discovered": discovered, "new": new, "skipped": skipped,
                "errors": errors, "note": note_parts}
    except Exception as e:
        db.finish_run(run_id, "error", discovered, new, errors + 1, None, f"fatal:{e}")
        raise


def run_jang(client=None) -> dict:
    """Kept for backward compatibility with earlier instructions."""
    return run_source(jang.SOURCE, client)


def run_all() -> dict:
    """Run every source one after another; one source failing does not stop the others."""
    out: dict[str, dict] = {}
    for name in SOURCES:
        try:
            out[name] = run_source(name)
        except Exception as e:
            log.exception("source %s crashed", name)
            out[name] = {"status": "crashed", "error": f"{type(e).__name__}: {e}"}
    return out
