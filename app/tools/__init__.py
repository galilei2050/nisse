"""Tool infrastructure: the process-wide ToolRegistry both the main agent and sub-agents build from."""

from app.tools.registry import ToolFactory, ToolRegistrar, ToolRegistry

# NB: `build_tool_registry` lives in `app.tools.wiring` and is imported from there directly (backend,
# probe) — NOT re-exported here. If this package __init__ imported wiring (which imports every domain),
# a domain importing `ToolRegistrar` from `app.tools` would cycle back through wiring. Keep __init__
# to the generic registry only.
__all__ = ["ToolFactory", "ToolRegistrar", "ToolRegistry"]
