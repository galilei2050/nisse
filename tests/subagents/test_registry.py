"""Tool registry: an unknown tool name in a config fails loud, not silently.

Only the guard is unit-tested here — building the real tools (and asserting the parent/child see the
right ones) is covered end-to-end by the probe against real deps, not with stubs. See tests/CLAUDE.md.
"""

import pytest

from app.subagents.registry import build_tools


def test_build_tools_rejects_unknown_name() -> None:
    """A config naming a tool outside the registry raises — validation runs before any tool is built."""
    with pytest.raises(ValueError, match="unknown tool names"):
        build_tools(["google_search", "not_a_real_tool"], deps=None)  # deps unused: the guard fires first
