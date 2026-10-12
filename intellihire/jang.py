"""Source adapter for https://jang.com.pk/jobs (newspaper job ads).

What the site looks like (observed Oct 2026):
  * /jobs and /jobs/latest list ads as links  /jobs/<id>-<slug>
  * a detail page has labelled fields (Date, City, Job Title, Organization, Apply)
    plus ONE image of the newspaper ad (og:image) -- the ad body is NOT text.
  * robots.txt allows all paths; no crawl-delay. We still stay slow and identify ourselves.

The parser is deliberately label-based and uses og: meta tags instead of CSS class names,
so it survives cosmetic template changes.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from . import common

SOURCE = "jang"
BASE = "https://jang.com.pk"
LISTING_PAGES = ["/jobs", "/jobs/latest"]
USER_AGENT = os.environ.get(
    "SCRAPER_USER_AGENT", "IntelliHire-FYP/0.1 (academic research; contact: set SCRAPER_CONTACT)"
)
DELAY_RANGE = (2.0, 4.0)  # seconds between requests

JOB_LINK = re.compile(r"^(?:https?://jang\.com\.pk)?/jobs/(\d+)-([a-z0-9\-]+)/?$", re.I)
LABELS = ("Date", "City", "Job Title", "Organization", "Apply")
PHONE_DIGITS = re.compile(r"\D+")


@dataclass
class Listing:
    source_id: str
    url: str


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})
    return s


def polite_sleep() -> None:
    common.polite_sleep()   # one shared delay for every source


def parse_listing(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Listing] = {}
    for a in soup.find_all("a", href=True):
        m = JOB_LINK.match(a["href"].strip())
        if m:
            sid = m.group(1)
            found[sid] = Listing(sid, f"{BASE}/jobs/{sid}-{m.group(2)}")
    return sorted(found.values(), key=lambda l: int(l.source_id), reverse=True)


def _meta(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return tag["content"].strip() if tag and tag.get("content") else None


def _labelled_fields(soup: BeautifulSoup) -> dict[str, str]:
    """Find 'Label' followed by its value, regardless of the surrounding markup."""
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines()]
    lines = [ln for ln in lines if ln]
    out: dict[str, str] = {}
    for i, ln in enumerate(lines[:-1]):
        if ln in LABELS and ln not in out:
            nxt = lines[i + 1]
            if nxt not in LABELS:
                out[ln] = nxt
    return out


def parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    for fmt in ("%d %B, %Y", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def hash_phone(raw: str, salt: str | None = None) -> tuple[str, str]:
    """Return (salted_hash, masked_display). Raw numbers are never stored."""
    digits = PHONE_DIGITS.sub("", raw)
    if digits.startswith("92") and len(digits) >= 12:
        digits = "0" + digits[2:]
    salt = salt if salt is not None else os.environ.get("PHONE_SALT", "dev-salt-change-me")
    h = hashlib.sha256((salt + digits).encode()).hexdigest()[:32]
    masked = (digits[:4] + "*" * max(len(digits) - 6, 0) + digits[-2:]) if len(digits) >= 7 else "***"
    return h, masked


def parse_detail(html: str, source_id: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    f = _labelled_fields(soup)
    title = f.get("Job Title") or (soup.find("h2").get_text(strip=True) if soup.find("h2") else None)
    phone_hash = phone_masked = None
    apply_raw = f.get("Apply")
    if apply_raw and re.search(r"\d{7,}", PHONE_DIGITS.sub("", apply_raw)):
        phone_hash, phone_masked = hash_phone(apply_raw)
    return {
        "source": SOURCE,
        "source_id": source_id,
        "url": url,
        "title": title,
        "organization": f.get("Organization"),
        "city": f.get("City"),
        "posted_date": parse_date(f.get("Date")),
        "apply_phone_hash": phone_hash,
        "apply_phone_masked": phone_masked,
        "image_url": _meta(soup, "og:image"),
        "ocr_text": None,
        "ocr_engine": None,
    }


def content_hash(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8", "ignore")).hexdigest()


class JangClient:
    # Ids grow with time, so a streak of old ads means everything further down is older still.
    early_stop = True

    def __init__(self) -> None:
        self.s = _session()

    def get(self, url: str) -> str:
        r = self.s.get(url, timeout=25)
        if r.status_code in (429, 503):
            raise RuntimeError(f"rate-limited or unavailable ({r.status_code}); stopping politely")
        r.raise_for_status()
        return r.text

    def discover(self) -> list[Listing]:
        seen: dict[str, Listing] = {}
        for path in LISTING_PAGES:
            for l in parse_listing(self.get(BASE + path)):
                seen[l.source_id] = l
            polite_sleep()
        return sorted(seen.values(), key=lambda l: int(l.source_id), reverse=True)

    def fetch_detail(self, listing: Listing) -> str:
        return self.get(listing.url)

    def download_image(self, url: str) -> bytes:
        r = self.s.get(url, timeout=30)
        r.raise_for_status()
        return r.content
