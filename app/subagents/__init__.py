"""Configurable sub-agents: per-conversation configs exposed to the main agent as delegating tools."""

from app.subagents.hypothesis_tree import HypothesisTreeTool
from app.subagents.registry import TOOL_REGISTRY, build_tools
from app.subagents.store import SubagentConfig, SubagentStore
from app.subagents.tool import SubagentTool

__all__ = [
    "TOOL_REGISTRY",
    "HypothesisTreeTool",
    "SubagentConfig",
    "SubagentStore",
    "SubagentTool",
    "build_tools",
]
