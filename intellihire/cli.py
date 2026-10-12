"""Command line helpers.

  python -m intellihire.cli run              run every source once
  python -m intellihire.cli run rozee        run one source
  python -m intellihire.cli probe rozee      fetch ONE page, save its HTML, show what the parser extracted
  python -m intellihire.cli probe rozee <url>
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import types

from . import pipeline


def probe(source: str, url: str | None) -> None:
    spec = pipeline.SOURCES[source]
    client = spec.client_factory()
    if url:
        ids = re.findall(r"(\d+)", url)
        listing = types.SimpleNamespace(source_id=ids[-1] if ids else "probe", url=url, lastmod=None)
    else:
        listings = client.discover()
        if not listings:
            print("discover() returned nothing: the source has no listings inside the freshness window.")
            return
        listing = listings[0]
    html = client.fetch_detail(listing)
    out = pathlib.Path("probe_out")
    out.mkdir(exist_ok=True)
    path = out / f"{source}_{listing.source_id}.html"
    path.write_text(html, encoding="utf-8")
    parsed = spec.parse(html, listing.source_id, listing.url)
    print(f"requested : {listing.url}")
    if getattr(client, "last_url", None):
        print(f"landed on : {client.last_url}")
    print(f"saved     : {path}  ({len(html):,} characters)")
    print(json.dumps(parsed, indent=2, default=str, ensure_ascii=False))
    empty = [k for k in ("title", "organization", "city", "posted_date", "deadline") if not parsed.get(k)]
    if empty:
        print(f"\nEMPTY FIELDS: {', '.join(empty)}  -> send the saved HTML file so the parser can be fixed")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="intellihire")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one or all sources")
    r.add_argument("source", nargs="?", default="all", choices=[*pipeline.SOURCES, "all"])
    p = sub.add_parser("probe", help="fetch one page and show the parsed fields")
    p.add_argument("source", choices=list(pipeline.SOURCES))
    p.add_argument("url", nargs="?")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        out = pipeline.run_all() if a.source == "all" else {a.source: pipeline.run_source(a.source)}
        print(json.dumps(out, indent=2, default=str))
    else:
        probe(a.source, a.url)


if __name__ == "__main__":
    main()
