"""Search — SerpApi-backed discovery and detail tools for the agent."""

from collections.abc import Callable

from baski.agents.tool import Tool
from baski.clients.serpapi_client import SerpApiClient

from app.search.tools import (
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

# Every SerpApi leaf, in one place — backend registers each under its own `.name`.
SEARCH_LEAVES: tuple[type[Tool], ...] = (
    GoogleSearchTool,
    GoogleAiModeTool,
    GoogleMapsSearchTool,
    GoogleNewsTool,
    GoogleEventsTool,
    AmazonSearchTool,
    AmazonProductTool,
    YouTubeSearchTool,
    YouTubeTranscriptTool,
    GoogleJobsTool,
)


def search_leaf(cls: type[Tool]) -> Callable[[CoreDeps, int], list[Tool]]:
    """A factory for one SerpApi search leaf — conversation-agnostic; a fresh client per build."""

    def build(deps: CoreDeps, _conversation_id: int) -> list[Tool]:
        return [cls(serpapi_client=SerpApiClient(http_client=deps.http))]

    return build


__all__ = [
    "SEARCH_LEAVES",
    "AmazonProductTool",
    "AmazonSearchTool",
    "GoogleAiModeTool",
    "GoogleEventsTool",
    "GoogleJobsTool",
    "GoogleMapsSearchTool",
    "GoogleNewsTool",
    "GoogleSearchTool",
    "YouTubeSearchTool",
    "YouTubeTranscriptTool",
    "search_leaf",
]
