"""SerpTool — abstract base for all SerpApi leaf tools, plus the shared `format_hits` helper."""

from abc import abstractmethod
from typing import Any, ClassVar

from baski.agents.tool import Tool
from baski.clients.serpapi_client import SerpApiClient
from pydantic import BaseModel


def format_hits(title: str, hits: list[dict[str, str]], *, limit: int = 5) -> str:
    """Up to `limit` hits as compact lines.

    Each hit = ordered {label: value}; empty/None values are dropped.
    First value is the title-ish field (unlabelled); the rest are `label: value`.
    One line per hit.
    """
    lines = [title]
    for i, hit in enumerate(hits[:limit], 1):
        head = next((v for v in hit.values() if v), "")  # title-ish field (first non-empty), unlabelled
        labelled = [f"{k}: {v}" for k, v in hit.items() if k and v]
        lines.append(f"{i}. " + " · ".join([head, *labelled] if head else labelled))
    return "\n".join(lines)


class _EmptyInput(BaseModel):
    """Placeholder Input for the abstract `SerpTool`; every concrete leaf overrides it."""


class SerpTool(Tool):
    """Base for one-shot SerpApi tools. A subclass adds an engine in ~15 lines.

    Subclass declares: name / one_line / description / Input (from Tool),
    plus `engine`, `params()`, `render()`.
    """

    engine: ClassVar[str]  # SerpApi engine id, e.g. "google_maps"

    # A concrete (empty) model so Tool.__init_subclass__ can derive its schema; typed as in baski's
    # Tool so each leaf's narrower Input is an accepted override, not a type conflict.
    Input: ClassVar[type[BaseModel]] = _EmptyInput

    def __init__(self, serpapi_client: SerpApiClient) -> None:
        """Store the SerpApi client."""
        self.serpapi = serpapi_client

    @abstractmethod
    def params(self, **kwargs: Any) -> dict:  # noqa: ANN401, ANON002 — abstract dispatch; SerpAPI params vary per engine
        """Map validated Input fields → SerpApi query params (q, k, v, asin, gl, hl, …)."""

    @abstractmethod
    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Map this engine's JSON to rows and call `format_hits`.

        Per-engine = which fields to pull; layout/truncation = shared helper.
        A discovery tool MUST surface the entity id its matching detail tool consumes
        (asin, video id) so the model can chain the two.
        """

    async def execute(self, **kwargs: Any) -> str:  # noqa: ANN401 — mirrors Tool.execute(**kwargs)
        """Call SerpApi and render; return a no-results note if the render is empty."""
        results = await self.serpapi.request("GET", self.engine, params=self.params(**kwargs))
        return self.render(results) or f"No {self.engine} results."
