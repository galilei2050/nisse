"""Tool wiring — build the process-wide ToolRegistry by letting each domain register its own tools.

Thin composition layer: it calls each domain's `register_tools(registrar)` — the domain owns HOW its
tools are built and under which names — like `backend.py` collecting routers. `backend.py` and the
probe both call `build_tool_registry()`, so registration lives in one place instead of being
duplicated per entry point. The generic `ToolRegistry` (registry.py) stays tool-agnostic.
"""

from app import browser, lists, memory, prompts, scheduling, search, subagents
from app.chat import ask
from app.tools.registry import ToolRegistry


def build_tool_registry() -> ToolRegistry:
    """Assemble the registry: each domain registers its own tools into it."""
    registry = ToolRegistry()
    search.register_tools(registry)
    memory.register_tools(registry)
    lists.register_tools(registry)
    scheduling.register_tools(registry)
    prompts.register_tools(registry)
    subagents.register_tools(registry)  # the researcher-only hypothesis tree
    ask.register_tools(registry)  # the clarifying-question tool (needs `deps.bot`)
    browser.register_tools(registry)  # `browser` — registered so the curator CAN grant it; nobody holds it
    return registry
