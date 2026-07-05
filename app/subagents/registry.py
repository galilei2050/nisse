"""Tool registry — the child-tool whitelist.

Maps a sub-agent config's `tool_names` to live Tool instances. Contains ONLY read-only web/browse
leaves — the same set `_build_web_tools` exposes — so a child can never get a state-writing,
sending, or delegating tool (single-writer; no sub-agent-in-sub-agent recursion). The registry IS
the whitelist: a config naming anything outside it fails loud at build.
"""

from collections.abc import Callable, Iterable

from baski.agents.tool import Tool
from baski.agents.tools import WebBrowseTool
from baski.clients.serpapi_client import SerpApiClient

from app.search import (
    AmazonProductTool,
    AmazonSearchTool,
    GoogleAiModeTool,
    GoogleEventsTool,
    GoogleJobsTool,
    GoogleMapsSearchTool,
    GoogleNewsTool,
    GoogleSearchTool,
    YouTubeSearchTool,
    YouTubeTranscriptTool,
)
from app.shared import CoreDeps

ToolFactory = Callable[[CoreDeps], Tool]


def _serp(deps: CoreDeps) -> SerpApiClient:
    """A SerpApi client over the shared http pool (a thin wrapper — cheap to build per tool)."""
    return SerpApiClient(http_client=deps.http)


# keys are each tool's real `.name` (app/search/tools.py, baski web_browse.py) — the tokens a config references
TOOL_REGISTRY: dict[str, ToolFactory] = {
    "google_search": lambda d: GoogleSearchTool(serpapi_client=_serp(d)),
    "google_ai_answer": lambda d: GoogleAiModeTool(serpapi_client=_serp(d)),
    "google_maps_search": lambda d: GoogleMapsSearchTool(serpapi_client=_serp(d)),
    "google_news": lambda d: GoogleNewsTool(serpapi_client=_serp(d)),
    "google_events": lambda d: GoogleEventsTool(serpapi_client=_serp(d)),
    "amazon_search": lambda d: AmazonSearchTool(serpapi_client=_serp(d)),
    "amazon_product": lambda d: AmazonProductTool(serpapi_client=_serp(d)),
    "youtube_search": lambda d: YouTubeSearchTool(serpapi_client=_serp(d)),
    "youtube_transcript": lambda d: YouTubeTranscriptTool(serpapi_client=_serp(d)),
    "google_jobs": lambda d: GoogleJobsTool(serpapi_client=_serp(d)),
    "browse_website": lambda d: WebBrowseTool(playwright_client=d.playwright),
}


def build_tools(names: Iterable[str], deps: CoreDeps) -> list[Tool]:
    """Build live tools for the given registry names; raise on any unknown name (bad config)."""
    names = list(names)
    unknown = set(names) - TOOL_REGISTRY.keys()
    if unknown:
        raise ValueError(f"unknown tool names (not in registry): {sorted(unknown)}")
    return [TOOL_REGISTRY[name](deps) for name in names]
