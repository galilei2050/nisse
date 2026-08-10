"""ToolRegistry — a name→factory catalog, populated at startup, queried to build tools.

Router-style: as an HTTP router maps a path to a handler, this maps a tool name to a factory that
builds the tool(s) for that name. Each domain registers its own tools via `register_tools(registrar)`,
called from `backend.py` (the composition root) — the registry itself imports no specific tool, so it
isn't the place that "knows all tools". Both the main Assistant and each sub-agent build their tools
through this one registry: the caller passes the names it wants (its spec), so "which agent gets which
tool" lives in the caller's spec, not in a flag here.

A factory takes `(deps, conversation_id)`: `deps` are the shared process clients; `conversation_id`
scopes the tools that bind a store to one chat (memory, lists, scheduling). Process-level tools (web
search, the hypothesis tree) ignore it. A factory returns a list so one name can yield several tools
(e.g. the four memory tools, or the hypothesis-tree pair).
"""

from collections.abc import Callable, Iterable
from typing import Protocol

from baski.agents.tool import Tool

from app.shared import CoreDeps

ToolFactory = Callable[[CoreDeps, int], list[Tool]]


class ToolRegistrar(Protocol):
    """What a domain's `register_tools()` consumes: register one tool factory by name.

    A Protocol (not the concrete `ToolRegistry`) so a domain depends only on the registration surface,
    not the whole registry — and never on `app.tools` importing the domain back.
    """

    def register(self, name: str, factory: ToolFactory) -> None:
        """Register one tool's factory under `name`."""
        ...


class ToolRegistry(ToolRegistrar):
    """The process-wide tool catalog. Lifecycle: long-lived — one per bot, built once at startup.

    Implements `ToolRegistrar` explicitly (not just structurally) so the type checker verifies its
    `register` keeps matching the Protocol the domains depend on.
    """

    def __init__(self) -> None:
        """Start empty; each domain's `register_tools` adds its factories (see `backend.py`)."""
        self._factories: dict[str, ToolFactory] = {}

    def register(self, name: str, factory: ToolFactory) -> None:
        """Register one tool's factory under its name; a duplicate name is a wiring bug — fail loud."""
        if name in self._factories:
            raise ValueError(f"tool '{name}' is already registered")
        self._factories[name] = factory

    def get(self, name: str) -> ToolFactory | None:
        """The factory for `name`, or None if nothing is registered under it (caller decides)."""
        return self._factories.get(name)

    def catalog(self, deps: CoreDeps, conversation_id: int) -> dict[str, list[str]]:
        """Every registered name mapped to the one-line summary of each tool it yields.

        For a reader that has to CHOOSE names — the curator deciding which tools a worker needs. Names
        alone are not enough to choose with: `browse_website` and a click-and-type browser both look
        like "the web" until you read what each one does, and a nightly pass that guesses wrong grants
        a capability the worker already had. Built from each `Tool.one_line`, so the description a
        chooser reads is the same text the agent holding the tool reads — never a second copy.
        """
        return {
            name: [tool.one_line for tool in factory(deps, conversation_id)]
            for name, factory in self._factories.items()
        }

    def build(self, names: Iterable[str], deps: CoreDeps, conversation_id: int) -> list[Tool]:
        """Build every tool named in `names` for one conversation; an unknown name fails loud."""
        tools: list[Tool] = []
        for name in names:
            factory = self._factories.get(name)
            if factory is None:
                raise ValueError(f"unknown tool '{name}' (not registered)")
            tools.extend(factory(deps, conversation_id))
        return tools
