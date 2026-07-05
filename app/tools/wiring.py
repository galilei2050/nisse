"""Tool wiring — register every tool's factory into a ToolRegistry, plus the main agent's tool spec.

This is the one place that names every tool and how to build it — the composition layer, like
`backend.py` collecting routers. It is kept OUT of the generic `ToolRegistry` (registry.py) so the
registry itself stays tool-agnostic. `backend.py` and the probe both build their registry here, so
registration lives in one place instead of being duplicated per entry point.
"""

from baski.agents.tool import Tool
from baski.agents.tools import WebBrowseTool

from app.lists import list_tools
from app.memory import memory_tools
from app.prompts import core_memory_tools
from app.scheduling import scheduling_tools
from app.search import SEARCH_LEAVES, search_leaf
from app.shared import CoreDeps
from app.subagents.hypothesis_tree import build_hypothesis_tree_tools
from app.tools.registry import ToolRegistry

# The main Assistant's tool spec — every registered tool EXCEPT the researcher-only hypothesis tree.
# "Which agent gets which tool" lives here (and in each sub-agent's config), not in a flag on the tool.
MAIN_TOOLS: list[str] = [
    *(cls.name for cls in SEARCH_LEAVES),
    "browse_website",
    "memory",
    "lists",
    "scheduling",
    "core_memory",
]


def _browse_tool(deps: CoreDeps, _conversation_id: int) -> list[Tool]:
    """Headless-browser page reader — conversation-agnostic."""
    return [WebBrowseTool(playwright_client=deps.playwright)]


def _hypothesis_tree(_deps: CoreDeps, _conversation_id: int) -> list[Tool]:
    """The researcher's granular hypothesis-tree pair over one fresh, ephemeral tree."""
    return build_hypothesis_tree_tools()


def build_tool_registry() -> ToolRegistry:
    """Register every tool's factory by name — the process-wide catalog, built once at startup."""
    registry = ToolRegistry()
    for cls in SEARCH_LEAVES:
        registry.register(cls.name, search_leaf(cls))
    registry.register("browse_website", _browse_tool)
    registry.register("memory", memory_tools)
    registry.register("lists", list_tools)
    registry.register("scheduling", scheduling_tools)
    registry.register("core_memory", core_memory_tools)
    registry.register("hypothesis_tree", _hypothesis_tree)
    return registry
