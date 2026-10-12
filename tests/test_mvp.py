import base64
import hashlib
import os
import time

import jwt
import pytest

LISTING_HTML = """
<html><body><ul>
<li><a href="https://jang.com.pk/jobs/72215-teachers">Teachers</a></li>
<li><a href="https://jang.com.pk/jobs/72208-web-developer">Web Developer</a></li>
<li><a href="/jobs/popular-cities/karachi">Jobs in Karachi</a></li>
<li><a href="https://jang.com.pk/jobs/72208-web-developer">dup</a></li>
</ul></body></html>"""

DETAIL_HTML = """
<html><head>
<meta property="og:image" content="https://jang.com.pk/assets/uploads/card_image/2023-10-16/72208_103509_card.png">
</head><body>
<h2>Web Developer</h2>
<ul>
 <li><span>Date</span><p>16 October, 2023</p></li>
 <li><span>City</span><p>Lahore</p></li>
 <li><span>Job Title</span><p>Web Developer</p></li>
 <li><span>Organization</span><a>Private Company</a></li>
 <li><span>Apply</span><p>0306-0675174</p></li>
</ul></body></html>"""


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    from intellihire import common, db
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("PHONE_SALT", "test-salt")
    monkeypatch.setattr(common, "polite_sleep", lambda: None)   # never really sleep in tests
    db.reset_engine()
    yield
    db.reset_engine()


def test_listing_parser_dedups_and_filters():
    from intellihire import jang
    ids = [l.source_id for l in jang.parse_listing(LISTING_HTML)]
    assert ids == ["72215", "72208"]


def test_detail_parser_fields_and_phone_privacy():
    from intellihire import jang
    p = jang.parse_detail(DETAIL_HTML, "72208", "https://jang.com.pk/jobs/72208-web-developer")
    assert (p["title"], p["city"], p["organization"]) == ("Web Developer", "Lahore", "Private Company")
    assert str(p["posted_date"]) == "2023-10-16"
    assert p["image_url"].endswith("72208_103509_card.png")
    assert "0675174" not in str(p) and "03060675174" not in str(p)  # raw number never kept
    assert p["apply_phone_masked"].startswith("0306") and p["apply_phone_hash"]


def test_phone_hash_is_stable_across_formats():
    from intellihire import jang
    a, _ = jang.hash_phone("0306-0675174")
    b, _ = jang.hash_phone("+92 306 0675174")
    assert a == b


def detail_dated(d):
    """DETAIL_HTML with its posting date replaced by d (a datetime.date)."""
    return DETAIL_HTML.replace("16 October, 2023", d.strftime("%d %B, %Y"))


class FakeClient:
    def __init__(self, detail_html=DETAIL_HTML):
        self.detail_html = detail_html
        self.fetches = 0

    def discover(self):
        from intellihire import jang
        return jang.parse_listing(LISTING_HTML)

    def fetch_detail(self, listing):
        self.fetches += 1
        return self.detail_html

    def download_image(self, url):
        raise AssertionError("OCR is off by default")


def test_freshness_rules():
    from datetime import date, timedelta
    from intellihire.freshness import freshness_reason as fr
    today = date(2026, 10, 11)
    assert fr(today - timedelta(days=3), today, days=60) is None
    assert fr(today - timedelta(days=60), today, days=60) is None        # boundary is inclusive
    assert fr(today - timedelta(days=61), today, days=60) == "too_old"
    assert fr(date(2023, 10, 16), today, days=60) == "too_old"
    assert fr(None, today, days=60) == "no_date"
    assert fr(today + timedelta(days=5), today, days=60) == "future_date"
    recent = today - timedelta(days=3)
    assert fr(recent, today, days=60, deadline=today - timedelta(days=1)) == "expired"
    assert fr(recent, today, days=60, deadline=today) is None             # last day to apply still counts
    assert fr(recent, today, days=60, deadline=today + timedelta(days=9)) is None
    # optional current-year rule: a late-December ad seen in early January
    jan = date(2027, 1, 5)
    assert fr(date(2026, 12, 28), jan, days=60, year_rule=False) is None
    assert fr(date(2026, 12, 28), jan, days=60, year_rule=True) == "wrong_year"


def test_pipeline_ingests_fresh_ads(monkeypatch):
    from datetime import date, timedelta
    from intellihire import db, jang, pipeline
    monkeypatch.setattr(jang, "polite_sleep", lambda: None)
    client = FakeClient(detail_dated(date.today() - timedelta(days=5)))
    r1 = pipeline.run_jang(client)
    assert r1["status"] == "ok" and r1["new"] == 2 and r1["skipped"] == {}
    assert not any("SOURCE_STALE" in n for n in r1["note"])
    r2 = pipeline.run_jang(client)
    assert r2["new"] == 0                      # nothing re-ingested
    assert len(db.list_postings(since=None)) == 2


def test_pipeline_skips_old_ads_and_never_refetches_them(monkeypatch):
    from intellihire import db, jang, pipeline
    monkeypatch.setattr(jang, "polite_sleep", lambda: None)
    client = FakeClient()                      # 2023 date -> outside any sane window
    r1 = pipeline.run_jang(client)
    assert r1["new"] == 0 and r1["skipped"] == {"too_old": 2}
    assert db.list_postings(since=None) == []  # nothing stored as a posting
    assert any("SOURCE_STALE" in n for n in r1["note"])
    fetched = client.fetches
    r2 = pipeline.run_jang(client)
    assert client.fetches == fetched           # skipped ids are remembered, not re-fetched


def test_pipeline_stops_early_when_archive_reached(monkeypatch):
    from intellihire import jang, pipeline
    monkeypatch.setattr(jang, "polite_sleep", lambda: None)
    monkeypatch.setattr(pipeline, "STALE_STREAK_STOP", 1)
    client = FakeClient()
    r = pipeline.run_jang(client)
    assert client.fetches == 1                 # stopped after the first out-of-window ad
    assert any("ARCHIVE_REACHED" in n for n in r["note"])


def test_read_time_filter_hides_postings_that_aged_out():
    from datetime import date, timedelta
    from intellihire import db, freshness, jang

    def post(sid, d):
        p = jang.parse_detail(detail_dated(d), sid, f"https://jang.com.pk/jobs/{sid}-x")
        db.upsert_posting(p)

    post("1", date.today() - timedelta(days=5))
    post("2", date.today() - timedelta(days=200))
    assert len(db.list_postings(since=freshness.cutoff_date(days=60))) == 1
    assert len(db.list_postings(since=None)) == 2              # include_archive behaviour
    assert len(db.list_postings(since=freshness.cutoff_date(days=365))) == 2


def test_api_auth_and_read(monkeypatch):
    from fastapi.testclient import TestClient
    from api import main
    from intellihire import pipeline

    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.setenv("QSTASH_CURRENT_SIGNING_KEY", "sig-key")
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://example.test")
    calls = []
    monkeypatch.setattr(pipeline, "run_source", lambda s, client=None: calls.append(s) or {"status": "ok"})
    monkeypatch.setattr(pipeline, "run_all", lambda: calls.append("all") or {})
    client = TestClient(main.app)
    bearer = {"Authorization": "Bearer s3cret"}

    assert client.post("/api/cron/scrape").status_code == 401
    assert client.post("/api/cron/scrape", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/cron/scrape", headers=bearer).status_code == 202
    assert calls == ["all"]
    assert client.post("/api/cron/scrape/rozee", headers=bearer).status_code == 202
    assert calls == ["all", "rozee"]
    assert client.post("/api/cron/scrape/nope", headers=bearer).status_code == 404
    assert client.post("/api/cron/scrape/nope").status_code == 401        # auth is checked before the source name

    def sign(url, body):
        now = int(time.time())
        return jwt.encode(
            {"iss": "Upstash", "sub": url, "exp": now + 300, "nbf": now - 5, "iat": now, "jti": "1",
             "body": base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("=")},
            "sig-key", algorithm="HS256")

    body = b'{"x":1}'
    token = sign("https://example.test/api/cron/scrape/rozee", body)
    ok = client.post("/api/cron/scrape/rozee", content=body, headers={"Upstash-Signature": token})
    assert ok.status_code == 202
    # a signature is bound to the exact URL and body it was issued for
    assert client.post("/api/cron/scrape", content=body, headers={"Upstash-Signature": token}).status_code == 401
    assert client.post("/api/cron/scrape/rozee", content=b'{"x":2}',
                       headers={"Upstash-Signature": token}).status_code == 401

    r = client.get("/api/postings").json()
    assert r["count"] == 0 and r["active_since"]
    assert set(client.get("/api/health").json()["sources"]) == {"jang", "rozee"}
    assert client.get("/api/health?source=nope").status_code == 404


# ------------------------------------------------------------------ Rozee

def _today():
    from datetime import date
    return date.today()


def rozee_html(posted=None, deadline=None, title="HR Executive", company="Nova Bazaar"):
    rows = [f"<li><span>Company</span><span>{company}</span></li>",
            "<li><span>Location</span><span>Sialkot, Pakistan</span></li>"]
    if posted:
        rows.append(f"<li><span>Published Date</span><span>{posted.strftime('%b %d, %Y')}</span></li>")
    if deadline:
        rows.append(f"<li><span>Apply By</span><span>{deadline.strftime('%b %d, %Y')}</span></li>")
    return (f"<html><body><h1>{title}</h1><ul>{''.join(rows)}</ul>"
            "<h2>Job Description</h2><p>Manage hiring.</p></body></html>")


ROZEE_LABEL_HTML = """
<html><head><title>HR Executive - Nova Bazaar</title></head><body>
<h1>HR Executive</h1>
<ul>
<li><span>Company</span><span>Nova Bazaar</span></li>
<li><span>Location</span><span>Sialkot, Pakistan</span></li>
<li><span>Work Arrangement</span><span>On-site</span></li>
<li><span>Published Date</span><span>Oct 11, 2026</span></li>
<li><span>Apply By</span><span>Nov 11, 2026</span></li>
<li><span>Experience</span><span>4 years</span></li>
</ul>
<h2>Job Description</h2>
<p>Looking for an HR executive to manage recruitment. Contact hr@novabazaar.example or 0300-1234567.</p>
<h2>Required Skills</h2><p>Talent Acquisition</p><p>Workforce Planning</p>
<h2>Nice-to-have skills</h2><p>HRIS</p>
</body></html>"""

ROZEE_JSONLD_HTML = """
<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Accountant",
 "datePosted":"2026-10-09","validThrough":"2026-11-09T00:00:00",
 "hiringOrganization":{"@type":"Organization","name":"Shahid Enterprises"},
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress","addressLocality":"Lahore","addressCountry":"PK"}},
 "baseSalary":{"@type":"MonetaryAmount","currency":"PKR",
               "value":{"@type":"QuantitativeValue","minValue":60000,"maxValue":80000,"unitText":"MONTH"}},
 "description":"<p>Maintain books.</p>","skills":["Excel","Tally"]}
</script></head><body><h1>ignored when JSON-LD is present</h1></body></html>"""


def test_parse_any_date_formats():
    from datetime import date
    from intellihire.common import parse_any_date as p
    assert p("Oct 11, 2026") == date(2026, 10, 11)
    assert p("11 October, 2026") == date(2026, 10, 11)
    assert p("2026-11-09T00:00:00") == date(2026, 11, 9)
    assert p("Nov 1st, 2026") == date(2026, 11, 1)
    assert p('"Oct 05, 2026"') == date(2026, 10, 5)
    assert p("garbage") is None and p(None) is None and p("") is None


def test_rozee_label_parser_and_pii_scrubbing():
    from datetime import date
    from intellihire import rozee
    p = rozee.parse_detail(ROZEE_LABEL_HTML, "1904920", "https://www.rozee.pk/nova-bazaar-hr-executive-jobs-1904920")
    assert (p["title"], p["organization"], p["city"]) == ("HR Executive", "Nova Bazaar", "Sialkot")
    assert p["posted_date"] == date(2026, 10, 11) and p["deadline"] == date(2026, 11, 11)
    assert p["experience"] == "4 years" and p["work_mode"] == "On-site"
    assert p["skills"] == "Talent Acquisition, Workforce Planning, HRIS"
    assert "hr@novabazaar" not in p["description"] and "1234567" not in p["description"]
    assert "[email removed]" in p["description"] and "[phone removed]" in p["description"]
    assert p["apply_phone_hash"] is None


def test_rozee_json_ld_parser():
    from datetime import date
    from intellihire import rozee
    p = rozee.parse_detail(ROZEE_JSONLD_HTML, "1903644", "https://www.rozee.pk/x-jobs-1903644")
    assert (p["title"], p["organization"], p["city"]) == ("Accountant", "Shahid Enterprises", "Lahore")
    assert p["posted_date"] == date(2026, 10, 9) and p["deadline"] == date(2026, 11, 9)
    assert p["salary"] == "60000-80000 PKR" and p["skills"] == "Excel, Tally"
    assert p["description"] == "Maintain books."


def test_rozee_sitemap_and_discovery_window(monkeypatch):
    import types
    from intellihire import rozee
    today = _today().isoformat()
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.rozee.pk/nova-bazaar-hr-executive-jobs-1904920</loc><lastmod>{today}</lastmod></url>
<url><loc>https://www.rozee.pk/old-company-clerk-jobs-1000001</loc><lastmod>2023-10-06</lastmod></url>
<url><loc>https://www.rozee.pk/weird-entry-no-id.php</loc><lastmod>{today}</lastmod></url>
<url><loc>https://evil.example.com/x-jobs-5</loc><lastmod>{today}</lastmod></url>
</urlset>"""
    ids = [l.source_id for l in rozee.parse_sitemap(xml.encode())]
    assert ids == ["1904920", "1000001"]                  # newest lastmod first; bad entries dropped
    client = rozee.RozeeClient()
    monkeypatch.setattr(rozee.RozeeClient, "_get", lambda self, url: types.SimpleNamespace(content=xml.encode()))
    assert [l.source_id for l in client.discover()] == ["1904920"]   # old lastmod filtered out


class _Resp:
    def __init__(self, status=200, headers=None, text="<html></html>"):
        self.status_code, self.headers, self.text = status, headers or {}, text
        self.content, self.encoding = text.encode(), "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")


def test_rozee_redirects_only_to_allowed_hosts(monkeypatch):
    import pytest as _pytest
    from intellihire import rozee
    c = rozee.RozeeClient()
    seq = iter([_Resp(302, {"Location": "https://www.rozeegpt.ai/seeker/x-1"}), _Resp(200, text="<html>job</html>")])
    monkeypatch.setattr(c.s, "get", lambda url, **kw: next(seq))
    assert c._get("https://www.rozee.pk/x-jobs-1").text == "<html>job</html>"
    assert c.last_url == "https://www.rozeegpt.ai/seeker/x-1"

    monkeypatch.setattr(c.s, "get", lambda url, **kw: _Resp(302, {"Location": "https://evil.example.com/steal"}))
    with _pytest.raises(ValueError):
        c._get("https://www.rozee.pk/x-jobs-2")
    with _pytest.raises(ValueError):
        c._get("http://www.rozee.pk/plain-http")             # https only
    monkeypatch.setattr(c.s, "get", lambda url, **kw: _Resp(429))
    with _pytest.raises(RuntimeError):
        c._get("https://www.rozee.pk/x-jobs-3")


class FakeRozeeClient:
    early_stop = False

    def __init__(self, pages):
        self.pages = pages

    def discover(self):
        from intellihire import rozee
        return [rozee.Listing(i, f"https://www.rozee.pk/x-jobs-{i}", _today()) for i in self.pages]

    def fetch_detail(self, listing):
        return self.pages[listing.source_id]


def test_pipeline_rozee_deadline_sitemap_fallback_and_no_early_stop(monkeypatch):
    from datetime import timedelta
    from intellihire import db, freshness, pipeline
    monkeypatch.setattr(pipeline, "STALE_STREAK_STOP", 1)    # would stop a Jang run; must not stop Rozee
    t = _today()
    client = FakeRozeeClient({
        "1": rozee_html(t - timedelta(days=3), deadline=t - timedelta(days=1)),   # deadline passed
        "2": rozee_html(t - timedelta(days=2), deadline=t + timedelta(days=20)),  # open
        "3": rozee_html(None, None),                                              # no date: use sitemap lastmod
    })
    r = pipeline.run_source("rozee", client)
    assert r["new"] == 2 and r["skipped"] == {"expired": 1}, r
    assert any("posted_date_from_sitemap=1" in n for n in r["note"])
    assert not any("ARCHIVE_REACHED" in n for n in r["note"])
    assert {p["source_id"] for p in db.list_postings(since=None, source="rozee")} == {"2", "3"}
    assert db.list_postings(source="jang", since=None) == []

    # a stored posting whose deadline passes later drops out of the active list but stays in the archive
    stale = rozee_html(t - timedelta(days=1), deadline=t - timedelta(days=1))
    from intellihire import rozee
    db.upsert_posting(rozee.parse_detail(stale, "9", "https://www.rozee.pk/x-jobs-9"))
    active = {p["source_id"] for p in db.list_postings(since=freshness.cutoff_date(), source="rozee")}
    assert active == {"2", "3"}
    assert "9" in {p["source_id"] for p in db.list_postings(since=None, source="rozee")}


def test_pipeline_refuses_pages_the_parser_cannot_read():
    from intellihire import db, pipeline
    client = FakeRozeeClient({"7": "<html><body>Access denied</body></html>"})
    r = pipeline.run_source("rozee", client)
    assert r["new"] == 0 and r["errors"] == 1 and r["status"] == "error"
    assert any("PARSER_EMPTY" in n for n in r["note"])
    assert "7" not in db.known_source_ids("rozee")            # not remembered, so it is retried after a fix


def test_run_source_rejects_unknown_source():
    import pytest as _pytest
    from intellihire import pipeline
    with _pytest.raises(ValueError):
        pipeline.run_source("linkedin")
