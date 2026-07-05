"""Search — SerpApi-backed discovery/detail tools (+ the baski web-browse tool), and their wiring."""

from collections.abc import Callable

from baski.agents.tool import Tool
from baski.agents.tools import WebBrowseTool
from baski.clients.serpapi_client import SerpApiClient

from app.search.tools import (
    AmazonProductTool,
    AmazonSearchTool,
    GoogleAiModeTool,
    GoogleEventsTool,
    GoogleFinanceTool,
    GoogleFlightsTool,
    GoogleHotelsTool,
    GoogleJobsTool,
    GoogleMapsReviewsTool,
    GoogleMapsSearchTool,
    GoogleNewsTool,
    GoogleScholarTool,
    GoogleSearchTool,
    YouTubeSearchTool,
    YouTubeTranscriptTool,
)
from app.shared import CoreDeps
from app.tools.registry import ToolRegistrar


def _serp_leaf(cls: type[Tool]) -> Callable[[CoreDeps, int], list[Tool]]:
    """A factory for one SerpApi search leaf — conversation-agnostic; a fresh client per build."""

    def build(deps: CoreDeps, _conversation_id: int) -> list[Tool]:
        return [cls(serpapi_client=SerpApiClient(http_client=deps.http))]

    return build


def _browse(deps: CoreDeps, _conversation_id: int) -> list[Tool]:
    """The headless-browser page reader (baski) — conversation-agnostic."""
    return [WebBrowseTool(playwright_client=deps.playwright)]


def register_tools(registrar: ToolRegistrar) -> None:
    """Register every web tool by name — one explicit line each (no sweep)."""
    registrar.register("google_search", _serp_leaf(GoogleSearchTool))
    registrar.register("google_ai_answer", _serp_leaf(GoogleAiModeTool))
    registrar.register("google_maps_search", _serp_leaf(GoogleMapsSearchTool))
    registrar.register("google_news", _serp_leaf(GoogleNewsTool))
    registrar.register("google_events", _serp_leaf(GoogleEventsTool))
    registrar.register("amazon_search", _serp_leaf(AmazonSearchTool))
    registrar.register("amazon_product", _serp_leaf(AmazonProductTool))
    registrar.register("youtube_search", _serp_leaf(YouTubeSearchTool))
    registrar.register("youtube_transcript", _serp_leaf(YouTubeTranscriptTool))
    registrar.register("google_jobs", _serp_leaf(GoogleJobsTool))
    registrar.register("google_flights", _serp_leaf(GoogleFlightsTool))
    registrar.register("google_hotels", _serp_leaf(GoogleHotelsTool))
    registrar.register("google_finance", _serp_leaf(GoogleFinanceTool))
    registrar.register("google_scholar", _serp_leaf(GoogleScholarTool))
    registrar.register("google_maps_reviews", _serp_leaf(GoogleMapsReviewsTool))
    registrar.register("browse_website", _browse)


__all__ = [
    "AmazonProductTool",
    "AmazonSearchTool",
    "GoogleAiModeTool",
    "GoogleEventsTool",
    "GoogleFinanceTool",
    "GoogleFlightsTool",
    "GoogleHotelsTool",
    "GoogleJobsTool",
    "GoogleMapsReviewsTool",
    "GoogleMapsSearchTool",
    "GoogleNewsTool",
    "GoogleScholarTool",
    "GoogleSearchTool",
    "YouTubeSearchTool",
    "YouTubeTranscriptTool",
    "register_tools",
]
