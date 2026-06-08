"""Web domain — Google search + page browsing tools."""

from baski.agents.tool import Tool
from baski.agents.tools import GoogleSearchTool, WebBrowseTool
from baski.clients.serpapi_client import SerpApiClient

from app.shared import CoreDeps


def provide(deps: CoreDeps) -> list[Tool]:
    """Build the web tools from shared clients (mid-level SerpAPI client built here)."""
    serpapi = SerpApiClient(logger=deps.logger, http_client=deps.http)
    return [
        GoogleSearchTool(serpapi_client=serpapi),
        WebBrowseTool(playwright_client=deps.playwright),
    ]
