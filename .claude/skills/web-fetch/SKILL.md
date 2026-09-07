---
name: web-fetch
description: "Research the web during dev — Google search + fetch pages to clean markdown, one command. Use when you need real external examples, docs, or current facts (finding test cases, checking a library, grounding a claim)."
allowed-tools: Bash(.claude/skills/web-fetch/web_fetch.py *), Bash(python3 .claude/skills/web-fetch/web_fetch.py *)
---

PURPOSE: pull real external content into the session so claims and test cases come from data, not memory. One keyed script covers search + fetch; built-in tools cover the rest.

## The script (search + fetch, one command)

```bash
.claude/skills/web-fetch/web_fetch.py -s "your query"          # Google (SerpAPI): title · url · snippet
.claude/skills/web-fetch/web_fetch.py -s "query" -n 12         # more results (default 8)
.claude/skills/web-fetch/web_fetch.py "https://site/page"      # page -> clean markdown (chrome stripped)
.claude/skills/web-fetch/web_fetch.py "https://site/page" --max-chars 0   # full page, no truncation
.claude/skills/web-fetch/web_fetch.py "https://api/x.json" --jq '.items[]|{id,name}'  # JSON API, jq-filtered
```

- Auth: `SERPAPI_API_KEY` is auto-loaded from `.env` (via `load_dotenv()`) — no setup.
- HTML → clean markdown (strips script/nav/footer/etc.), capped at 20k chars by default (`--max-chars 0` for full).
- JSON responses pretty-print; pass `--jq` to extract only what you need (don't dump whole APIs into context).
- Fails loud on HTTP errors (no silent empty results).

## When to use the built-in tools instead

- `WebFetch(url, prompt=...)` — when you want the page **AI-extracted/summarized** per a focused prompt rather than raw markdown.
- Perplexity (`perplexity_ask` quick+cited · `perplexity_research` deep multi-source, 30s+) — synthesis with citations across many sources in one call.

Rough rule: raw page text or a known JSON API → **this script**; "search Google" → **this script** (`-s`);
"answer a question with sources" → **Perplexity**; "AI-summarize one page" → **WebFetch**.
