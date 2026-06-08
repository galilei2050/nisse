"""Tool registry — each domain contributes a provider; the toolset iterates these."""

from collections.abc import Callable

from baski.agents.tool import Tool

from app.shared import CoreDeps

from . import web

ToolProvider = Callable[[CoreDeps], list[Tool]]

# Add a domain by appending its provider here — backend wiring stays untouched.
PROVIDERS: list[ToolProvider] = [web.provide]

__all__ = ["PROVIDERS", "ToolProvider"]
