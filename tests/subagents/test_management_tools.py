"""`subagent_list` renders the roster AND the shelf a roster can be built from.

Why the shelf is the behaviour worth pinning: without it the tool shows only the names a worker
ALREADY holds, so a worker that cannot do something is indistinguishable from one that can. Measured on
the real pass (2026-08-09, the SFO parking window): the curator read `retrieval`'s `browse_website`,
concluded "the tool was there", left the roster alone, and instead wrote a core-memory rule ordering the
assistant to check live availability — which nothing it held could do.

The fake registry below carries deliberately fake summaries. What each real tool advertises is asserted
against the real classes in `test_registry.py`; here the subject is the rendering — order, and that a
name the save would refuse is never offered.
"""

from types import SimpleNamespace
from typing import cast

import pytest

from app.shared import CoreDeps
from app.subagents.store import SubagentConfig, SubagentStore
from app.subagents.tools import NOT_GRANTABLE, SubagentListTool

_OFFERED = "browse_website"


class _Registry:
    """Stands in for ToolRegistry: `_catalogue` only calls `catalog`."""

    def catalog(self, _deps: object, _conversation_id: int) -> dict[str, list[str]]:
        """Every refused name, plus one offered name with two tools under it."""
        catalog: dict[str, list[str]] = {name: [f"summary of {name}"] for name in NOT_GRANTABLE}
        catalog[_OFFERED] = ["first thing it does", "second thing it does"]
        return catalog


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
        tool_names=["google_search", _OFFERED],
        context_tokens=180000,
        max_turns=8,
        judge_prompt="claims tied to named sources",
    )


@pytest.mark.asyncio
async def test_every_tool_under_a_name_is_listed_with_what_it_does() -> None:
    """A name alone cannot be chosen on, and one name can yield several tools — keep them all."""
    rendered = await _tool([_config()]).execute()
    assert f"- {_OFFERED}: first thing it does · second thing it does" in rendered


@pytest.mark.asyncio
async def test_the_roster_comes_first_and_whole() -> None:
    """The shelf is an addition, not a replacement — the config's own text is what gets edited."""
    rendered = await _tool([_config()]).execute()
    assert rendered.index("### retrieval") < rendered.index("Registered tools you may grant")
    assert f"tool_names: google_search, {_OFFERED}" in rendered


@pytest.mark.asyncio
async def test_nothing_the_save_would_refuse_is_offered() -> None:
    """Offering a name `_reject` refuses would spend a nightly turn on a rejected save.

    Looped over the constant rather than spelled out: the two sides agreeing is the whole point, so a
    name added to `NOT_GRANTABLE` must be covered here without anyone remembering to edit this test.
    """
    rendered = await _tool([_config()]).execute()
    for name in NOT_GRANTABLE:
        assert f"- {name}:" not in rendered


@pytest.mark.asyncio
async def test_the_shelf_shows_even_with_no_workers_configured() -> None:
    """A fresh conversation has no roster, and that is exactly when knowing what exists matters."""
    rendered = await _tool([]).execute()
    assert "No sub-agents are configured" in rendered
    assert f"- {_OFFERED}:" in rendered


@pytest.mark.asyncio
async def test_says_a_worker_name_is_also_a_valid_entry() -> None:
    """The shelf is registered tools only; delegating to a sibling is the other half of `tool_names`.

    Presented as complete, it would tell the pass to stop when the answer is "have one worker call
    another" — which is exactly the move that closes a gap no single tool covers.
    """
    rendered = await _tool([_config()]).execute()
    assert "may ALSO be the name of one of the workers above" in rendered
