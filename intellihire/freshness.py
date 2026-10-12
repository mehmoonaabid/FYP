"""Freshness rules: is a posting recent enough, and still open, to be treated as active?

We cannot trust a source's own "still open" status (ads are often never closed), so
activeness is derived from dates. The same rules are applied twice:
  * at ingest  (pipeline.py): ads failing the rules are never stored as postings
  * at read    (db.list_postings / API): postings that age out or pass their deadline drop out

Rules:
  1. posting date must exist, not be in the future, and be within MAX_AGE_DAYS of today
  2. if the source gives an apply-by deadline, it must not have passed
  3. optionally (REQUIRE_CURRENT_YEAR=1) the posting year must equal the current year

Env vars (read at call time):
  MAX_AGE_DAYS=60            window size; use 30 for one month
  REQUIRE_CURRENT_YEAR=0     1 = also demand the posting year equals the current year
                             (off by default: it would wrongly drop a 28 December ad in early January)
"""
from __future__ import annotations

import os
from datetime import date, timedelta


def max_age_days() -> int:
    return int(os.environ.get("MAX_AGE_DAYS", "60"))


def require_current_year() -> bool:
    return os.environ.get("REQUIRE_CURRENT_YEAR", "0") == "1"


def cutoff_date(today: date | None = None, days: int | None = None) -> date:
    """Oldest posting date that still counts as active."""
    return (today or date.today()) - timedelta(days=max_age_days() if days is None else days)


def freshness_reason(
    posted: date | None,
    today: date | None = None,
    days: int | None = None,
    year_rule: bool | None = None,
    *,
    deadline: date | None = None,
) -> str | None:
    """Return None if the posting is fresh and open, otherwise a short reason code."""
    today = today or date.today()
    days = max_age_days() if days is None else days
    year_rule = require_current_year() if year_rule is None else year_rule

    if posted is None:
        return "no_date"            # cannot prove it is active -> do not treat it as active
    if posted > today + timedelta(days=1):
        return "future_date"        # bad data or parsing error
    if (today - posted).days > days:
        return "too_old"
    if year_rule and posted.year != today.year:
        return "wrong_year"
    if deadline is not None and deadline < today:
        return "expired"            # apply-by date has passed
    return None
