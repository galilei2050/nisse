# SerpApi search tools — design

How nisse exposes many SerpApi engines to the agent through one shared mechanism, chosen so
the assistant can do **more than the owner's daily routine** — research a topic from
influencer videos, read a product before buying, find a concert — not just mirror what he
already browses. Without bloating the agent's context or its decision space.

## Problem

The owner's Chrome history (`docs/chrome-history-facts.md`) is search-heavy: Maps is the
biggest Google route (1637), Search #5 (1015), YouTube #11 (585), Amazon #13 (550). Today
the agent has one SerpApi tool wired (`GoogleSearchTool`).

But the goal isn't to replay the history — it's to **unlock workflows the owner doesn't do
by hand because they're tedious**: pull an influencer's video transcript and summarize it,
read an Amazon product card, scan today's news, check what concerts are on. That reframes
selection from "top domains" to **capability chains**: a *discovery* tool finds entities, a
*detail* tool goes deep on one. They're worth more together than either alone.

All SerpApi engines are structurally identical — one authenticated GET to
`serpapi.com/search?engine=…`, JSON back, render to text — so the mechanism is shared. Two
constraints shape it:

1. **Context budget.** Every tool ships `name` + `description` + JSON input schema on
   *every* turn, plus a roster line (`ToolSet.system_prompt`). SerpApi has ~90 engines;
   exposing all would add ~13k tokens of permanent prompt and bury the signal.
2. **Model legibility (the priority).** The model picks by reading names + descriptions.
   Fewer, sharper, non-overlapping tools → more reliable selection. The selection list, not
   a clever encoding, is what keeps both costs down.

## Where the code lives

Split along the baski/Assistant line: **generic transport stays in baski; the tool family
lives in nisse.**

- **baski** keeps only `SerpApiClient` (`baski/clients/serpapi_client.py`) — a transport
  primitive (auth + HTTP + the generic `request`), per the "foundation primitives come from
  baski" rule. Unchanged.
- **nisse** owns the `SerpTool` base **and** every leaf, in a new `app/search/` domain
  package (alongside `app/memory/`, `app/scheduling/`). *Which* engines to expose and *how to
  render* them for this owner is a product decision — "because owner", not generic — so it
  belongs to the app, not the shared lib.

baski's existing `GoogleSearchTool` is re-expressed as a nisse leaf so all ten share one base
(see APIs table); nisse stops importing it from baski.

## Architecture

```
        Telegram (owner)
              │  text
              ▼
┌──────────────────────────────── nisse ──────────────────────────────────┐
│  Assistant → Conversations → Conversation → Agent (baski loop)           │
│                   │ builds once per chat                                  │
│                   ▼                                                       │
│              ToolSet  (per conversation)                                  │
│   ┌──────────────┬──────────┬─────────────┬───────────────────┐          │
│   │ web tools    │ memory   │ scheduling  │ short-term/history│          │
│   │ (_build_web) │ tools    │ tools       │                   │          │
│   └──────┬───────┴──────────┴─────────────┴───────────────────┘          │
│          │ app/search/                                                    │
│          ▼                                                                │
│   SerpTool (base):  params() → render()                                  │
│    ├─ google_search       ├─ amazon_search ──→ amazon_product            │
│    ├─ google_ai_mode      ├─ youtube_search ─→ youtube_transcript        │
│    ├─ google_maps_search  ├─ google_news                                 │
│    ├─ google_events       └─ google_jobs        (+ WebBrowse, baski)     │
└──────────┼───────────────────────────────────────────────────────────────┘
           │ SerpApiClient.request("GET", engine, params)
           ▼
┌──────────────────────────────── baski ──────────────────────────────────┐
│  SerpApiClient  — auth + HTTP + generic request()  ─────────────────────┼─► HTTPS
└──────────────────────────────────────────────────────────────────────────┘
                                                            serpapi.com/search?engine=…
```

`──→` = discovery→detail chain (a leaf's `render()` surfaces the id the next leaf consumes).

## Folder structure

```
nisse/                                  # the tool family lives here
├── app/
│   ├── search/                         # NEW domain package (like app/memory, app/scheduling)
│   │   ├── __init__.py                 # exports the 10 tool classes        (NEW)
│   │   ├── serp_tool.py                # SerpTool base (params/render)       (NEW)
│   │   └── tools.py                    # the 10 leaves, one class each       (NEW)
│   └── assistant/
│       └── conversations.py            # _build_web_tools(): register 10     (edit)
└── docs/
    └── serpapi-search-tools.md         # this design

baski/                                  # shared lib — transport only
└── clients/
    └── serpapi_client.py               # auth + HTTP + generic request()    (unchanged)
```

`tools.py` holds all ten leaves together (nisse's convention — `app/memory/tools.py` and
`app/scheduling/tools.py` each group a domain's tools in one file); the base sits beside them
in `serp_tool.py`. baski is untouched. Adding an engine = one class in `tools.py` + one line
in `_build_web_tools`.

## Class structure

### `SerpTool` — the family base (nisse, `app/search/serp_tool.py`)

Factors out the lifecycle every SerpApi tool runs — validate `Input` (already done by
`ToolSet`) → map fields to params → one `request` → render. A concrete engine declares only
what differs: the engine id, `params()`, `render()`. Covers both roles: a *discovery* tool's
`Input` is a free query, a *detail* tool's `Input` is an entity id — mechanically identical.
It subclasses baski's `Tool` and calls baski's `SerpApiClient` — the only two baski imports.

```python
# app/search/serp_tool.py
from abc import abstractmethod
from typing import Any, ClassVar

from baski.agents.tool import Tool
from baski.clients.serpapi_client import SerpApiClient


class SerpTool(Tool):
    """Base for one-shot SerpApi tools. A subclass adds an engine in ~15 lines.

    Subclass declares: name / one_line / description / Input (from Tool),
    plus `engine`, `params()`, `render()`.
    """

    engine: ClassVar[str]  # SerpApi engine id, e.g. "google_maps"

    def __init__(self, serpapi_client: SerpApiClient) -> None:
        self.serpapi = serpapi_client

    @abstractmethod
    def params(self, **kwargs: Any) -> dict:
        """Map validated Input fields → SerpApi query params (q, k, v, asin, gl, hl, …)."""

    @abstractmethod
    def render(self, results: dict) -> str:
        """Map this engine's JSON to rows and hand them to `format_hits` (the shared,
        token-tuned layout). Per-engine = which fields to pull; layout/truncation = shared.
        A discovery tool MUST surface the entity id its matching detail tool consumes
        (asin, video id) so the model can chain the two.
        """

    async def execute(self, **kwargs: Any) -> str:  # type: ignore[override]
        results = await self.serpapi.request("GET", self.engine, params=self.params(**kwargs))
        return self.render(results) or f"No {self.engine} results."
```

`Tool.__init_subclass__` already enforces a nested `Input`, so a malformed engine tool fails
at import, not runtime.

### Two leaves — a discovery/detail chain

```python
# discovery: find videos, surface the id the transcript tool needs
class YouTubeSearchTool(SerpTool):
    name = "youtube_search"
    one_line = "Search YouTube for videos"
    description = "Find videos by query (creators, topics, tutorials). Returns title, channel, views, and video id."
    engine = "youtube"

    class Input(BaseModel):
        query: str = Field(description="What to search for on YouTube")

    def params(self, query: str) -> dict:
        return {"search_query": query, "gl": "us", "hl": "en"}

    def render(self, results: dict) -> str:
        ...  # per video: title / channel / views / **video id** (so youtube_transcript can use it)


# detail: go deep on one video
class YouTubeTranscriptTool(SerpTool):
    name = "youtube_transcript"
    one_line = "Get the transcript of a YouTube video"
    description = "Fetch the full transcript of a video by its id (from youtube_search). Use to study/summarize its content."
    engine = "youtube_video_transcript"

    class Input(BaseModel):
        video_id: str = Field(description="YouTube video id (the v= value), e.g. from youtube_search")
        language_code: str = Field(default="en", description="Transcript language; defaults to en")

    def params(self, video_id: str, language_code: str) -> dict:
        return {"v": video_id, "language_code": language_code}

    def render(self, results: dict) -> str:
        ...  # join transcript segments into plain text
```

Every other engine is the same shape with a different `engine`, `Input`, `params`, `render`.
That is the "одним махом": the family is the mechanism; each engine is a thin leaf.

### `SerpApiClient` — unchanged

The client already exposes the generic `request("GET", engine, params=…)` the base calls.
A new engine touches **zero** client code. Existing typed helpers stay for other callers.

### Wiring (nisse)

`Conversations._build_web_tools` is the single selection point — the roster is exactly this
list, nothing more:

```python
from app.search import (
    AmazonProductTool, AmazonSearchTool, GoogleAiModeTool, GoogleEventsTool, GoogleJobsTool,
    GoogleMapsSearchTool, GoogleNewsTool, GoogleSearchTool, YouTubeSearchTool, YouTubeTranscriptTool,
)

def _build_web_tools(self) -> list[Tool]:
    serpapi = SerpApiClient(logger=self._deps.logger, http_client=self._deps.http)
    return [
        GoogleSearchTool(serpapi_client=serpapi),
        GoogleAiModeTool(serpapi_client=serpapi),
        GoogleMapsSearchTool(serpapi_client=serpapi),
        GoogleNewsTool(serpapi_client=serpapi),
        GoogleEventsTool(serpapi_client=serpapi),
        AmazonSearchTool(serpapi_client=serpapi),
        AmazonProductTool(serpapi_client=serpapi),
        YouTubeSearchTool(serpapi_client=serpapi),
        YouTubeTranscriptTool(serpapi_client=serpapi),
        GoogleJobsTool(serpapi_client=serpapi),
        WebBrowseTool(playwright_client=self._deps.playwright),
    ]
```

## APIs to implement

Engine ids and key params below are **verified against each engine's SerpApi doc page**
(June 2026). `[D]` discovery, `[T]` detail; chains are paired.

| Tool | `engine` | key param | role | Why |
|------|----------|-----------|------|-----|
| `google_search` | `google` | `q` | [D] | Search #5 (1015) — port baski's tool to a leaf |
| `google_ai_mode` | `google_ai_mode` | `q` | [D] | requested; synthesized answer + sources (not raw links) |
| `google_maps_search` | `google_maps` | `q` (+`ll`) | [D] | Maps #2 (1637), local-business habit |
| `google_news` | `google_news` | `q` | [D] | requested; scan headlines on demand |
| `google_events` | `google_events` | `q` | [D] | requested; concerts/events ("concerts in SF this weekend") |
| `amazon_search` | `amazon` | `k` | [D] | Amazon #13 (550); finds products + **asin** |
| `amazon_product` | `amazon_product` | `asin` | [T] | requested; product card — price, rating, specs |
| `youtube_search` | `youtube` | `search_query` | [D] | YouTube #11 (585); finds videos + **video id** |
| `youtube_transcript` | `youtube_video_transcript` | `v` | [T] | requested; study influencer/creator content |
| `google_jobs` | `google_jobs` | `q` (+`location`) | [D] | SWE/freelance profile (Upwork, LinkedIn in history) |

**The chains** (why the detail tools earn their slot):
- `youtube_search` → `youtube_transcript`: find an influencer's video, then read/summarize
  it without watching. The owner's stated use case.
- `amazon_search` → `amazon_product`: find a product, then pull its full card before buying.

**`google_ai_mode` vs `google_search`** (both take `q` — descriptions must split them so the
model chooses right): AI Mode returns one synthesized answer + sources (`reconstructed_markdown`),
best for "explain / compare / what's the best…" questions; `google_search` returns raw organic
links, best when the owner wants the sources themselves or a specific site/page.

**Deferred** (keep the roster sharp; add on real need):
- `youtube_video` (`youtube_video`, param `v`) — richer video metadata (likes, description).
  Transcript is the asked-for depth; add this only if metadata alone is needed.
- Maps place details / reviews (`google_maps_reviews` — client method already exists) —
  add a detail tool if "find a shop" routinely needs review text.
- Flights / Hotels (`google_flights`, `google_hotels`) — `google.com/travel` is low (125).
- Yelp (`yelp` — already in baski) — Maps supersedes it for this owner (not in top-30).

**Beyond SerpApi — a Perplexity research tool (separate integration).** Perplexity is *not*
a SerpApi engine, so it's not a `SerpTool` leaf — it's its own tool over its own client
(`PerplexityClient`, same shape as `SerpApiClient`: a baski transport primitive). It earns a
slot because it does the one thing this set can't: deep, multi-source, *cited* research that
synthesizes across pages, where `google_ai_mode` gives a single-pass answer and `google_search`
gives raw links. Wire it for "research X thoroughly / compare options with sources" requests
(`perplexity_research` for depth, `perplexity_ask` for a quick cited answer). Lives next to the
search tools in `app/search/` (or its own `app/research/`); same `Tool` contract, different
backend.

## Why this is optimal

**Context cost stays bounded.** Ten search tools ≈ ten roster lines + ten schemas ≈
~1.4k tokens, fixed per turn. All ~90 engines would be ~13k and a confused model. **Selection**
keeps it lean, and `_build_web_tools` is the one obvious place selection happens — to add or
drop an engine, edit one list.

**Detail tools are why the assistant exceeds the daily routine.** The owner already searches
Maps and Amazon by hand; a tool that only mirrors that adds little. The leverage is in the
chains he *doesn't* do manually — transcript extraction, product-card pulls — which turn
"search" into "research and decide." That's the point of the expanded set.

**One tool per engine beats a mega-`search(source=…)` tool — for the model.** A unified tool
saves ~700 tokens but forces the model to learn which params (and which entity ids) are valid
per source, making param/source mismatch a new error class. Distinct named tools each carry a
tailored `Input` the model reads at face value, and detail tools' id parameters make the
discovery→detail chain explicit in the schema. Clarity of selection is the stated priority;
the few hundred tokens buy it.

**Adding an engine is a leaf change.** New engine = one subclass (engine id + `Input` +
`params` + `render`) + one line in `_build_web_tools`. No client change, no base change, no
risk to existing tools. The abstraction is justified by ten concrete users today — reuse,
not speculation.

**It matches the codebase.** `app/search/` is a domain package like `app/memory/` and
`app/scheduling/` (each with its own `tools.py`); the leaves use the same `Tool` contract and
`serpapi.request` path the existing tools already use. The base just removes the copy-paste
between them. The split — transport in baski, engine selection + rendering in nisse — is the
same generic-vs-"because-owner" line the rest of the app follows.

## Rendering: how JSON becomes tokens (shared format, per-engine extraction)

The single biggest token lever isn't markup — it's **dropping 95% of the JSON**. A raw SerpApi
response is deeply nested and mostly noise (pagination, ad blocks, serpapi metadata, dozens of
fields per hit). So `render()` does two things, and they split cleanly:

- **What to extract — per engine.** Each engine's JSON shape differs (`organic_results` vs
  `local_results` vs `reviews` vs a transcript blob), so each leaf knows its own field paths.
  This *cannot* be shared. It's ~5 lines per leaf: pick top-N items, pull ~4 fields each, and
  the chain id (`asin`, `v`).
- **How to lay it out — shared.** One helper in `serp_tool.py` turns "a title + a list of rows"
  into compact lines. Duplicating layout across ten leaves would mean ten places to re-tune
  token cost; one helper means one.

```python
# app/search/serp_tool.py — shared, token-tuned in one place
def format_hits(title: str, hits: list[dict[str, str]], *, limit: int = 5) -> str:
    """Up to `limit` hits as compact lines. Each hit = ordered {label: value};
    empty/None values are dropped (no 'rating: None' noise). One line per hit."""
    lines = [title]
    for i, hit in enumerate(hits[:limit], 1):
        head = next((v for v in hit.values() if v), "")          # title-ish field (first non-empty), unlabelled
        labelled = [f"{k}: {v}" for k, v in hit.items() if k and v]
        lines.append(f"{i}. " + " · ".join([head, *labelled] if head else labelled))
    return "\n".join(lines)
```

```python
# a leaf just maps its JSON → rows, then calls the shared helper
def render(self, results: dict) -> str:
    hits = [
        {"": r.get("title"), "rating": r.get("rating"), "reviews": r.get("reviews"), "asin": r.get("asin")}
        for r in results.get("organic_results", [])
    ]
    return format_hits("Amazon results", hits) or "No Amazon results."
```

**The format, and why it's token-lean** — example output:
```
Amazon results
1. Anker USB-C 100W charger · rating: 4.6 · reviews: 12,400 · asin: B0ABC123
2. UGREEN 65W GaN charger · rating: 4.7 · reviews: 8,210 · asin: B0DEF456
```
- **One line per hit, ` · ` separators** — no JSON braces/quotes, no per-field newlines. ~2–3×
  fewer tokens than pretty-printed JSON or a multi-line block per hit.
- **No bold / no `#` headers in the body** — `**` and `#` each cost tokens and buy nothing for
  a model reading the text; plain `label: value` is read just as well.
- **Empty fields dropped** — a hit with no rating simply omits it, instead of `rating: null`.
- **Hard top-N (default 5)** — the truncation, not the styling, is what bounds the cost; the
  limit lives in the shared helper so every engine inherits it.
- **Chain ids surfaced as a labelled field** (`asin: …`, `v: …`) so the model can pass them to
  the paired detail tool — token-cheap and the thing that makes chaining work.

**The one exception — `youtube_transcript`** isn't a list of hits; it renders the joined
transcript text directly (no helper), deliberately large because studying the content is the
whole point. That's the only leaf that doesn't go through `format_hits`.
