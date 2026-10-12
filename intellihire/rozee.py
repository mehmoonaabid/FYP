"""Source adapter for Rozee.pk job postings.

What was observed (Oct 2026), through a summarising fetch tool, NOT raw HTML:
  * https://www.rozee.pk/sitemap/jobs.xml lists ~400 job URLs with <lastmod>, refreshed daily.
    URL pattern: https://www.rozee.pk/{employer}-{title}-jobs-{numeric id}
  * a job URL answers 302 to https://www.rozeegpt.ai/seeker/<slug>-<other id>; that page is
    server-rendered: title, company, city, "Published Date", "Apply By", experience, description, skills.
  * robots.txt on both hosts does not block job pages and sets no crawl-delay.
  * Rozee's terms allow educational use but not jobs/API abuse: stay slow, identify yourself, do not
    republish their listings (link back instead).

Because the real markup is unverified, parsing tries JSON-LD first and then falls back to label
matching. Run `python -m intellihire.cli probe rozee` to save one page and see what was parsed.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import common, freshness

SOURCE = "rozee"
SITEMAP_URL = "https://www.rozee.pk/sitemap/jobs.xml"
ALLOWED_HOSTS = ("rozee.pk", "rozeegpt.ai")   # we only ever visit these (and their subdomains)
MAX_REDIRECTS = 5
MAX_DESCRIPTION_CHARS = 6000


@dataclass
class Listing:
    source_id: str
    url: str
    lastmod: date | None = None


def host_allowed(url: str) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


# ---------------------------------------------------------------- discovery

def parse_sitemap(xml: str | bytes) -> list[Listing]:
    """Job listings from the sitemap, newest lastmod first. Entries without a numeric id are ignored."""
    root = ET.fromstring(xml)
    found: dict[str, Listing] = {}
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "url":
            continue
        loc = lastmod = None
        for ch in el:
            tag = ch.tag.rsplit("}", 1)[-1]
            if tag == "loc":
                loc = (ch.text or "").strip()
            elif tag == "lastmod":
                lastmod = common.parse_any_date((ch.text or "").strip())
        if not loc or not host_allowed(loc):
            continue
        m = re.search(r"-jobs-(\d+)", urlparse(loc).path)
        if m:
            found[m.group(1)] = Listing(m.group(1), loc, lastmod)
    return sorted(found.values(), key=lambda l: (l.lastmod or date.min, int(l.source_id)), reverse=True)


class RozeeClient:
    # Sitemap order is by lastmod, not by posting date, so an expired ad says nothing about the next
    # one: the pipeline must not stop early on a streak of skipped ads.
    early_stop = False

    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": common.USER_AGENT, "Accept-Language": "en"})
        self.last_url: str | None = None

    def _get(self, url: str) -> requests.Response:
        """GET following redirects by hand, so we can only ever land on allow-listed hosts."""
        for _ in range(MAX_REDIRECTS + 1):
            if not host_allowed(url):
                raise ValueError(f"refusing to fetch non-allowed host: {url}")
            r = self.s.get(url, timeout=25, allow_redirects=False)
            if r.status_code in (429, 503):
                raise RuntimeError(f"rate-limited or unavailable ({r.status_code}); stopping politely")
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                url = urljoin(url, r.headers["Location"])
                continue
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = "utf-8"
            self.last_url = url
            return r
        raise ValueError("too many redirects")

    def discover(self) -> list[Listing]:
        listings = parse_sitemap(self._get(SITEMAP_URL).content)
        cutoff = freshness.cutoff_date()
        # lastmod is "last modified", so an old lastmod proves the ad is old; a new one proves nothing.
        return [l for l in listings if l.lastmod is None or l.lastmod >= cutoff]

    def fetch_detail(self, listing) -> str:
        return self._get(listing.url).text


# ---------------------------------------------------------------- parsing

POSTED_LABELS = ("published date", "posted date", "date posted", "posted on", "published on", "published")
DEADLINE_LABELS = ("apply by", "apply before", "last date to apply", "last date", "deadline",
                   "closing date", "valid through")
EXPERIENCE_LABELS = ("experience", "years of experience", "experience required",
                     "minimum experience", "min experience")
LOCATION_LABELS = ("location", "city", "job location")
COMPANY_LABELS = ("company", "organization", "organisation", "hiring organization", "employer")
SALARY_LABELS = ("salary", "salary range", "compensation")
MODE_LABELS = ("work arrangement", "work mode", "workplace", "work type", "job type")
SKILL_LABELS = ("required skills", "skills required", "key skills", "skills")
NICE_SKILL_LABELS = ("nice-to-have skills", "nice to have skills", "preferred skills")
DESC_LABELS = ("job description", "description", "summary", "about the role", "about this job", "role summary")
RESP_LABELS = ("key responsibilities", "job responsibilities", "responsibilities")
OTHER_HEADINGS = ("requirements", "benefits", "apply now", "apply", "share", "similar jobs",
                  "about the company", "about company", "qualifications")

ALL_LABELS = frozenset(
    POSTED_LABELS + DEADLINE_LABELS + EXPERIENCE_LABELS + LOCATION_LABELS + COMPANY_LABELS + SALARY_LABELS
    + MODE_LABELS + SKILL_LABELS + NICE_SKILL_LABELS + DESC_LABELS + RESP_LABELS + OTHER_HEADINGS
)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<![\w])\+?\d[\d\s\-().]{7,}\d(?![\w])")


def scrub_pii(text: str | None) -> str | None:
    """Remove e-mail addresses and phone numbers: we do not store personal contact data."""
    if not text:
        return text
    text = EMAIL_RE.sub("[email removed]", text)
    return PHONE_RE.sub(lambda m: "[phone removed]" if sum(c.isdigit() for c in m.group()) >= 9 else m.group(),
                        text)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().strip('"').strip()


def _key(line: str) -> str:
    return line.lower().rstrip(":").strip()


def _lines(soup: BeautifulSoup) -> list[str]:
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    out = [_norm(x) for x in soup.get_text("\n").splitlines()]
    return [x for x in out if x]


def _find_value(lines: list[str], labels) -> str | None:
    labels = set(labels)
    for i, ln in enumerate(lines):
        if _key(ln) in labels:
            for nxt in lines[i + 1:i + 3]:
                if _key(nxt) not in ALL_LABELS:
                    return _norm(nxt)
        elif ":" in ln:
            head, _, tail = ln.partition(":")
            if _key(head) in labels and tail.strip():
                return _norm(tail)
    return None


def _find_section(lines: list[str], labels, max_lines: int = 60) -> list[str]:
    labels = set(labels)
    for i, ln in enumerate(lines):
        if _key(ln) in labels:
            block: list[str] = []
            for nxt in lines[i + 1:i + 1 + max_lines]:
                if _key(nxt) in ALL_LABELS:
                    break
                block.append(nxt)
            if block:
                return block
    return []


def _iter_nodes(data):
    if isinstance(data, dict):
        yield data
        for v in data.get("@graph", []) if isinstance(data.get("@graph"), list) else []:
            yield from _iter_nodes(v)
    elif isinstance(data, list):
        for v in data:
            yield from _iter_nodes(v)


def _json_ld_job(soup: BeautifulSoup) -> dict | None:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except ValueError:
            continue
        for node in _iter_nodes(data):
            t = node.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                return node
    return None


def _first(x):
    return x[0] if isinstance(x, list) and x else x


def _from_json_ld(node: dict) -> dict:
    org = node.get("hiringOrganization")
    loc = _first(node.get("jobLocation"))
    addr = loc.get("address") if isinstance(loc, dict) else None
    city = None
    if isinstance(addr, dict):
        city = addr.get("addressLocality") or addr.get("addressRegion")
    sal = node.get("baseSalary")
    salary = None
    if isinstance(sal, dict):
        v = sal.get("value")
        cur = sal.get("currency", "")
        if isinstance(v, dict):
            lo, hi = v.get("minValue"), v.get("maxValue")
            salary = f"{lo}-{hi} {cur}".strip() if lo or hi else (str(v.get("value")) if v.get("value") else None)
        elif v:
            salary = f"{v} {cur}".strip()
    skills = node.get("skills")
    if isinstance(skills, list):
        skills = ", ".join(map(str, skills))
    exp = node.get("experienceRequirements")
    if isinstance(exp, dict):
        months = exp.get("monthsOfExperience")
        exp = f"{int(months) // 12} years" if months else None
    desc = node.get("description")
    if desc:
        desc = _norm(BeautifulSoup(str(desc), "html.parser").get_text(" "))
    mode = "Remote" if node.get("jobLocationType") == "TELECOMMUTE" else None
    return {
        "title": node.get("title"),
        "organization": org.get("name") if isinstance(org, dict) else (org if isinstance(org, str) else None),
        "city": city,
        "posted": common.parse_any_date(str(node.get("datePosted") or "")),
        "deadline": common.parse_any_date(str(node.get("validThrough") or "")),
        "salary": salary,
        "skills": skills,
        "experience": exp if isinstance(exp, str) else None,
        "description": desc,
        "work_mode": mode,
    }


def _clean_city(loc: str | None) -> str | None:
    if not loc:
        return None
    first = loc.split(",")[0].strip()
    return None if not first or first.lower() == "pakistan" else first


def _meta(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return _norm(tag["content"]) if tag and tag.get("content") else None


def parse_detail(html: str, source_id: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    ld = _from_json_ld(_json_ld_job(soup) or {})
    meta_title, meta_desc = _meta(soup, "og:title"), (_meta(soup, "og:description") or _meta(soup, "description"))
    h1 = soup.find("h1")
    h1_text = _norm(h1.get_text(" ")) if h1 else None
    lines = _lines(soup)

    title = ld["title"] or h1_text or meta_title
    org = ld["organization"] or _find_value(lines, COMPANY_LABELS)
    city = _clean_city(ld["city"]) or _clean_city(_find_value(lines, LOCATION_LABELS))
    posted = ld["posted"] or common.parse_any_date(_find_value(lines, POSTED_LABELS))
    deadline = ld["deadline"] or common.parse_any_date(_find_value(lines, DEADLINE_LABELS))
    experience = ld["experience"] or _find_value(lines, EXPERIENCE_LABELS)
    salary = ld["salary"] or _find_value(lines, SALARY_LABELS)

    mode = ld["work_mode"] or _find_value(lines, MODE_LABELS)
    if not mode:
        mode = next((ln for ln in lines if re.fullmatch(r"(?i)on-?site|remote|hybrid", ln)), None)
    if mode:
        m = re.search(r"(?i)on-?site|remote|hybrid", mode)
        mode = {"onsite": "On-site", "on-site": "On-site"}.get(m.group().lower(), m.group().title()) if m else mode

    skills = ld["skills"]
    if not skills:
        parts = _find_section(lines, SKILL_LABELS) + _find_section(lines, NICE_SKILL_LABELS)
        skills = ", ".join(parts) if parts else None

    description = ld["description"]
    if not description:
        parts = _find_section(lines, DESC_LABELS) + _find_section(lines, RESP_LABELS)
        description = "\n".join(parts) if parts else meta_desc
    description = scrub_pii(description)
    if description and len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS]

    return {
        "source": SOURCE, "source_id": source_id, "url": url,
        "title": title, "organization": org, "city": city,
        "posted_date": posted, "deadline": deadline,
        "experience": experience, "salary": salary, "work_mode": mode,
        "skills": scrub_pii(skills), "description": description,
        # Rozee shows no direct phone/e-mail; nothing personal is kept either way.
        "apply_phone_hash": None, "apply_phone_masked": None,
        "image_url": None, "ocr_text": None, "ocr_engine": None,
    }
