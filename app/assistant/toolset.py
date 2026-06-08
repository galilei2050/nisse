"""Toolset assembly — flattens every domain provider into the agent's tool list."""

from baski.agents.tool import Tool

from app.shared import CoreDeps
from app.tools import PROVIDERS


def build_tools(deps: CoreDeps) -> list[Tool]:
    """Collect tools from every registered domain provider into one flat list."""
    return [tool for provide in PROVIDERS for tool in provide(deps)]
