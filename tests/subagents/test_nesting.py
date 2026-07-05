"""Depth-1 nesting: the tool resolver delegates one level, then caps. Pure logic, no live deps.

Only the resolver's routing + the one-level cap are unit-tested (deps are never touched on these
paths). Whether a nested sub-agent actually runs is covered by the probe. See tests/CLAUDE.md.
"""

import pytest

from app.subagents import HypothesisTreeTool, SubagentConfig, SubagentTool


def _config(name: str, tool_names: list[str]) -> SubagentConfig:
    """A minimal valid config; only name/tool_names matter for resolution."""
    return SubagentConfig(
        conversation_id=1,
        name=name,
        description="x",
        system_prompt="x",
        model="claude-sonnet-4-6",
        tool_names=tool_names,
        context_tokens=1000,
        judge_prompt="x",
    )


def _tool(config: SubagentConfig, siblings: dict[str, SubagentConfig], *, can_delegate: bool) -> SubagentTool:
    return SubagentTool(config, deps=None, siblings=siblings, can_delegate=can_delegate)  # deps unused on these paths


def test_hypothesis_tree_resolves_to_a_fresh_tool() -> None:
    """The literal 'hypothesis_tree' name maps to an ephemeral HypothesisTreeTool."""
    orchestrator = _tool(_config("researcher", ["hypothesis_tree"]), {}, can_delegate=True)
    resolved = orchestrator._resolve_tool("hypothesis_tree")
    assert isinstance(resolved, HypothesisTreeTool)


def test_orchestrator_delegates_to_sibling_and_child_is_a_capped_leaf() -> None:
    """can_delegate resolves a sibling into a child; the child is a leaf that can't delegate again."""
    worker = _config("retrieval", ["google_search"])
    orchestrator = _tool(_config("researcher", ["retrieval"]), {"retrieval": worker}, can_delegate=True)
    child = orchestrator._resolve_tool("retrieval")
    assert isinstance(child, SubagentTool)
    # The child is a leaf: empty siblings + can_delegate=False → it can't resolve any sub-agent name.
    with pytest.raises(ValueError, match="delegation not allowed"):
        child._resolve_tool("retrieval")


def test_leaf_referencing_a_subagent_raises_loud() -> None:
    """A worker (can_delegate=False) naming a sibling is a seed error — fail loud, not a silent leaf."""
    orchestrator = _tool(_config("worker", ["researcher"]), {"researcher": _config("researcher", [])}, can_delegate=False)
    with pytest.raises(ValueError, match="unknown tool 'researcher'"):
        orchestrator._resolve_tool("researcher")
