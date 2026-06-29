#!/usr/bin/env python3
"""Keyless web research: fetch a URL as clean markdown, or search the web.

One command, no API keys. For dev research (finding examples, reading docs, grounding claims).
  web_fetch.py <url>                 # page -> clean markdown (HTML stripped of chrome)
  web_fetch.py <url> --jq '.x[]'     # JSON API -> filtered through jq
  web_fetch.py -s "query"            # Google results (SerpAPI): title · url · snippet
"""

import argparse
import json
import os
import re
import subprocess
import sys

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from markdownify import markdownify

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
_STRIP = ["script", "style", "nav", "header", "footer", "noscript", "svg", "form", "aside", "iframe"]


def _get(url: str, params: dict | None = None) -> httpx.Response:
    """GET with a real UA, following redirects; raises on HTTP error (fail loud)."""
    r = httpx.get(url, params=params, headers={"User-Agent": UA}, follow_redirects=True, timeout=30.0)
    r.raise_for_status()
    return r


def _run_jq(raw: str, expr: str) -> None:
    """Filter a JSON string through jq and print it."""
    out = subprocess.run(["jq", expr], input=raw, text=True, capture_output=True, check=True)
    print(out.stdout, end="")


def fetch(url: str, jq_expr: str | None, max_chars: int) -> None:
    """Fetch a URL: JSON gets pretty-printed (or jq-filtered), HTML becomes clean markdown."""
    r = _get(url)
    ctype = r.headers.get("content-type", "")

    if "json" in ctype or (jq_expr and "html" not in ctype):
        if jq_expr:
            _run_jq(r.text, jq_expr)
        else:
            print(json.dumps(json.loads(r.text), indent=2, ensure_ascii=False))
        return

    soup = BeautifulSoup(r.text, "lxml")
    for tag in soup(_STRIP):
        tag.decompose()
    md = markdownify(str(soup.body or soup), heading_style="ATX").strip()
    md = re.sub(r"\n{3,}", "\n\n", md)  # markdownify leaves runs of blank lines
    if max_chars and len(md) > max_chars:
        md = md[:max_chars] + f"\n\n…[truncated at {max_chars} chars — re-run with --max-chars 0 for full]"
    print(md)


def search(query: str, n: int) -> None:
    """Google results via SerpAPI (key from .env). Prints `title · url` + snippet per hit."""
    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        print("SERPAPI_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)
    r = _get("https://serpapi.com/search", params={"engine": "google", "q": query, "num": n, "api_key": api_key})
    for res in r.json().get("organic_results", [])[:n]:
        print(f"\n• {res.get('title', '')}\n  {res.get('link', '')}")
        if res.get("snippet"):
            print(f"  {res['snippet']}")


def main() -> None:
    """Parse args and dispatch to search or fetch."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", nargs="?", help="URL to fetch")
    p.add_argument("-s", "--search", metavar="QUERY", help="Search the web instead of fetching a URL")
    p.add_argument("-n", type=int, default=8, help="Number of search results (default 8)")
    p.add_argument("-j", "--jq", help="Filter a JSON response through this jq expression")
    p.add_argument("--max-chars", type=int, default=20000, help="Truncate markdown output (0 = no limit)")
    args = p.parse_args()
    load_dotenv()  # SERPAPI_API_KEY for search

    if args.search:
        search(args.search, args.n)
    elif args.url:
        fetch(args.url, args.jq, args.max_chars)
    else:
        p.error("provide a URL or -s QUERY")


if __name__ == "__main__":
    main()
