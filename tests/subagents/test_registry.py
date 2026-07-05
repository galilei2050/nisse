"""ToolRegistry (app/tools): register/get/build routing + loud on duplicate and unknown names.

Also guards the one audience invariant that has no flag to enforce it: the researcher-only
`hypothesis_tree` must not be in the main Assistant's spec. Pure logic, no live deps.
"""

import pytest

from app.tools import MAIN_TOOLS, ToolRegistry


def test_build_flattens_registered_factories_in_order() -> None:
    """build() calls each named factory with (deps, conversation_id) and concatenates their tools."""
    registry = ToolRegistry()
    registry.register("a", lambda _deps, _cid: ["a1", "a2"])
    registry.register("b", lambda _deps, _cid: ["b1"])
    assert registry.build(["a", "b"], deps=None, conversation_id=1) == ["a1", "a2", "b1"]


def test_duplicate_registration_is_loud() -> None:
    """Registering the same name twice is a wiring bug — fail loud, don't silently overwrite."""
    registry = ToolRegistry()
    registry.register("a", lambda _deps, _cid: [])
    with pytest.raises(ValueError, match="already registered"):
        registry.register("a", lambda _deps, _cid: [])


def test_build_unknown_name_is_loud() -> None:
    """A spec naming a tool that was never registered raises before anything is built."""
    with pytest.raises(ValueError, match="unknown tool"):
        ToolRegistry().build(["nope"], deps=None, conversation_id=1)


def test_get_returns_none_for_unregistered() -> None:
    """get() is the soft lookup the sub-agent resolver uses to fall through to sibling delegation."""
    assert ToolRegistry().get("nope") is None


def test_hypothesis_tree_is_not_a_main_tool() -> None:
    """The researcher's hypothesis tree must never reach the main Assistant (owner's rule)."""
    assert "hypothesis_tree" not in MAIN_TOOLS
