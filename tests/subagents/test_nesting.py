"""Depth-1 nesting: the tool resolver builds from the registry, delegates one level, then caps.

Pure logic, no live deps — a stub `deps` carries a registry with just the tools these paths touch;
its clients are never used. Whether a nested sub-agent actually runs is covered by the probe.
"""

from types import SimpleNamespace

import pytest

from app.subagents import AddHypothesisTool, SubagentConfig, SubagentTool, UpdateHypothesisTool
from app.subagents.hypothesis_tree import build_hypothesis_tree_tools
from app.tools import ToolRegistry


def _deps() -> SimpleNamespace:
    """A stub deps whose only used attribute is `.tools` — a registry with just `hypothesis_tree`."""
    registry = ToolRegistry()
    registry.register("hypothesis_tree", lambda _deps, _conversation_id: build_hypothesis_tree_tools())
    return SimpleNamespace(tools=registry)


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
        max_turns=5,
        judge_prompt="x",
    )


def _tool(config: SubagentConfig, siblings: dict[str, SubagentConfig]) -> SubagentTool:
    return SubagentTool(config, _deps(), conversation_id=1, siblings=siblings)


def test_hypothesis_tree_expands_to_the_granular_pair() -> None:
    """The registered 'hypothesis_tree' name expands to the ephemeral add/update tools over one tree."""
    orchestrator = _tool(_config("researcher", ["hypothesis_tree"]), {})
    resolved = orchestrator._resolve_tools("hypothesis_tree")
    assert [type(t) for t in resolved] == [AddHypothesisTool, UpdateHypothesisTool]


def test_orchestrator_delegates_to_sibling_and_child_is_a_capped_leaf() -> None:
    """A name not in the registry but a sibling resolves to a child; the child (no siblings) can't again."""
    worker = _config("retrieval", ["google_search"])
    orchestrator = _tool(_config("researcher", ["retrieval"]), {"retrieval": worker})
    (child,) = orchestrator._resolve_tools("retrieval")
    assert isinstance(child, SubagentTool)
    # The child is a leaf: no siblings → it can't resolve any sub-agent name.
    with pytest.raises(ValueError, match="delegation not allowed"):
        child._resolve_tools("retrieval")


def test_leaf_referencing_a_subagent_raises_loud() -> None:
    """A leaf (no siblings) naming an unregistered tool is a seed error — fail loud, not a silent leaf."""
    leaf = _tool(_config("worker", ["researcher"]), {})
    with pytest.raises(ValueError, match="neither a registered tool nor a delegable sibling"):
        leaf._resolve_tools("researcher")
