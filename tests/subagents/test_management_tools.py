"""`subagent_list` renders the roster AND the shelf a roster can be built from.

Why the shelf is the behaviour worth pinning: without it the tool shows only the names a worker
ALREADY holds, so a worker that cannot do something is indistinguishable from one that can. Measured
on the real pass (2026-08-09, the SFO parking window): the curator read `retrieval`'s `browse_website`,
concluded "the tool was there", left the roster alone, and instead wrote a core-memory rule ordering
the assistant to check live availability — which nothing it held could do.
"""

from types import SimpleNamespace
from typing import cast

import pytest

from app.shared import CoreDeps
from app.subagents.store import SubagentConfig, SubagentStore
from app.subagents.tools import SubagentListTool


class _Registry:
    """Stands in for ToolRegistry: `_catalogue` only calls `catalog`."""

    def catalog(self, _deps: object, _conversation_id: int) -> dict[str, list[str]]:
        return {
            "browse_website": ["Browse and extract content from any website"],
            "browser": ["Open a URL in your logged-in browser session", "Click an element by its [ref]"],
            "judge_rules": ["Edit the rules added to the rubric"],
            "subagents": ["Show the configured sub-agents", "Create or update one sub-agent"],
        }


class _Store:
    """Stands in for SubagentStore: `execute` only calls `list()`."""

    def __init__(self, configs: list[SubagentConfig]) -> None:
        self._configs = configs

    async def list(self) -> list[SubagentConfig]:
        return self._configs


def _tool(configs: list[SubagentConfig]) -> SubagentListTool:
    deps = cast("CoreDeps", SimpleNamespace(tools=_Registry()))
    return SubagentListTool(cast("SubagentStore", _Store(configs)), deps, conversation_id=7)


def _config() -> SubagentConfig:
    return SubagentConfig(
        conversation_id=7,
        name="retrieval",
        description="answers one self-contained sub-question",
        system_prompt="cited, compressed",
        model="moonshotai/kimi-k2-thinking",
        tool_names=["google_search", "browse_website"],
        context_tokens=180000,
        max_turns=8,
        judge_prompt="claims tied to named sources",
    )


@pytest.mark.asyncio
async def test_lists_the_grantable_tools_with_what_each_one_does() -> None:
    """A name alone cannot be chosen on: the summary is what separates fetching a page from acting on one."""
    rendered = await _tool([_config()]).execute()
    assert "browser: Open a URL in your logged-in browser session · Click an element by its [ref]" in rendered
    assert "browse_website: Browse and extract content from any website" in rendered


@pytest.mark.asyncio
async def test_the_roster_still_comes_first_and_whole() -> None:
    """The catalogue is an addition, not a replacement — the config's own text is what gets edited."""
    rendered = await _tool([_config()]).execute()
    assert rendered.index("### retrieval") < rendered.index("Tools available to grant")
    assert "tool_names: google_search, browse_website" in rendered


@pytest.mark.asyncio
async def test_curator_only_tools_are_not_offered() -> None:
    """`subagent_save` refuses these in a tool_names, so listing them would only invite a rejected save."""
    rendered = await _tool([_config()]).execute()
    assert "judge_rules:" not in rendered
    assert "subagents:" not in rendered


@pytest.mark.asyncio
async def test_the_shelf_shows_even_with_no_workers_configured() -> None:
    """A fresh conversation has no roster, and that is exactly when knowing what exists matters."""
    rendered = await _tool([]).execute()
    assert "No sub-agents are configured" in rendered
    assert "browser: Open a URL" in rendered
