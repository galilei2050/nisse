"""Configurable sub-agents: per-conversation configs exposed to the main agent as delegating tools."""

from baski.agents.tools import ShortTermMemory

from app.subagents.hypothesis_tree import (
    AddHypothesisTool,
    HypothesisStatus,
    UpdateHypothesisTool,
    build_hypothesis_tree_tools,
)
from app.subagents.store import SubagentConfig, SubagentStore
from app.subagents.tool import SubagentTool
from app.tools.registry import ToolRegistrar


def register_tools(registrar: ToolRegistrar) -> None:
    """Register the sub-agent-facing tools into the shared registry (a fresh instance per run).

    - `hypothesis_tree`: the researcher's add/update pair over one ephemeral tree.
    - `short_term`: a `ShortTermMemory` (baski `working_note`) scratchpad — a multi-step sub-agent
      (the researcher) keeps findings across turns in it, the tier the main agent hand-wires. The
      main agent does NOT resolve it here (it keeps its own instance to clear per reply); only a
      sub-agent whose `tool_names` lists `short_term` gets one.
    """
    registrar.register("hypothesis_tree", lambda _deps, _conversation_id: build_hypothesis_tree_tools())
    registrar.register("short_term", lambda _deps, _conversation_id: [ShortTermMemory()])


__all__ = [
    "AddHypothesisTool",
    "HypothesisStatus",
    "SubagentConfig",
    "SubagentStore",
    "SubagentTool",
    "UpdateHypothesisTool",
    "build_hypothesis_tree_tools",
    "register_tools",
]
