"""Helpers shared by all source adapters."""
from __future__ import annotations

import hashlib
import os
import random
import re
import time
from datetime import date, datetime

USER_AGENT = os.environ.get(
    "SCRAPER_USER_AGENT",
    "IntelliHire-FYP/0.1 (academic research; contact: set SCRAPER_USER_AGENT)",
)
DELAY_RANGE = (2.0, 4.0)  # seconds between requests: stay slow, stay polite


def polite_sleep() -> None:
    time.sleep(random.uniform(*DELAY_RANGE))


def content_hash(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8", "ignore")).hexdigest()


_DATE_FORMATS = (
    "%b %d, %Y", "%B %d, %Y", "%d %B, %Y", "%d %B %Y", "%d %b, %Y", "%d %b %Y",
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
)


def parse_any_date(raw: str | None) -> date | None:
    """Parse the date formats seen on Pakistani job sites. Returns None if unrecognised."""
    if not raw:
        return None
    s = raw.strip().strip('"').replace(" ", " ")
    s = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", s)      # "Nov 1st, 2026"
    s = s.replace("Sept", "Sep")
    if re.match(r"\d{4}-\d{2}-\d{2}T", s):              # ISO datetime from JSON-LD
        s = s[:10]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None
