"""ToolRegistry — a name→factory catalog, populated at startup, queried to build tools.

Router-style: as an HTTP router maps a path to a handler, this maps a tool name to a factory that
builds the tool(s) for that name. Factories are registered one-by-one in `backend.py` (the
composition root) — the registry itself imports no specific tool, so it isn't the place that "knows
all tools". Both the main Assistant and each sub-agent build their tools through this one registry:
the caller passes the names it wants (its spec), so "which agent gets which tool" lives in the
caller's spec, not in a flag here.

A factory takes `(deps, conversation_id)`: `deps` are the shared process clients; `conversation_id`
scopes the tools that bind a store to one chat (memory, lists, scheduling). Process-level tools (web
search, the hypothesis tree) ignore it. A factory returns a list so one name can yield several tools
(e.g. the four memory tools, or the hypothesis-tree pair).
"""

from collections.abc import Callable, Iterable

from baski.agents.tool import Tool

from app.shared import CoreDeps

ToolFactory = Callable[[CoreDeps, int], list[Tool]]


class ToolRegistry:
    """The process-wide tool catalog. Lifecycle: long-lived — one per bot, built once at startup."""

    def __init__(self) -> None:
        """Start empty; `backend.py` registers each tool's factory by name."""
        self._factories: dict[str, ToolFactory] = {}

    def register(self, name: str, factory: ToolFactory) -> None:
        """Register one tool's factory under its name; a duplicate name is a wiring bug — fail loud."""
        if name in self._factories:
            raise ValueError(f"tool '{name}' is already registered")
        self._factories[name] = factory

    def get(self, name: str) -> ToolFactory | None:
        """The factory for `name`, or None if nothing is registered under it (caller decides)."""
        return self._factories.get(name)

    def build(self, names: Iterable[str], deps: CoreDeps, conversation_id: int) -> list[Tool]:
        """Build every tool named in `names` for one conversation; an unknown name fails loud."""
        tools: list[Tool] = []
        for name in names:
            factory = self._factories.get(name)
            if factory is None:
                raise ValueError(f"unknown tool '{name}' (not registered)")
            tools.extend(factory(deps, conversation_id))
        return tools
