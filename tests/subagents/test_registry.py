"""ToolRegistry (app/tools): register/get/build routing + loud on duplicate and unknown names.

Also guards the one audience invariant that has no flag to enforce it: the researcher-only
`hypothesis_tree` must not be in the main Assistant's spec. Pure logic, no live deps.
"""

import pytest

from app.assistant.conversations import MAIN_TOOLS
from app.tools import ToolRegistry
from app.tools.wiring import build_tool_registry


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


def test_main_spec_is_general_web_only_no_hypothesis_tree() -> None:
    """Main gets general web search + the state tools — NOT the hypothesis tree nor specialized leaves."""
    assert "hypothesis_tree" not in MAIN_TOOLS  # researcher-only (owner's rule)
    assert "google_search" in MAIN_TOOLS  # the general web search is on the main agent
    assert "amazon_search" not in MAIN_TOOLS  # specialized leaves stay off the always-on roster


def test_browser_is_registered_but_not_on_the_main_roster() -> None:
    """`browser` must be grantable and un-held: the curator can only give a tool the registry knows.

    Both halves are the point. Unregistered, `subagent_save` refuses the name and the nightly pass
    cannot act on evidence that a worker needs to click; on `MAIN_TOOLS`, it would be handed out by
    this commit rather than by the pass that read the evidence.
    """
    assert "browser" not in MAIN_TOOLS
    assert build_tool_registry().get("browser") is not None


class _Stub:
    """A tool as `catalog` reads it: only `one_line` is touched."""

    def __init__(self, one_line: str) -> None:
        self.one_line = one_line


def test_catalog_maps_each_name_to_the_one_line_of_every_tool_it_yields() -> None:
    """The chooser's view: one entry per registry NAME, carrying each tool's own summary text.

    A name can yield several tools, and the summaries must stay per-tool — collapsing them would hide
    that `browser` covers clicking AND typing, which is the distinction the reader is choosing on.
    """
    registry = ToolRegistry()
    registry.register("pair", lambda _deps, _cid: [_Stub("does A"), _Stub("does B")])
    registry.register("single", lambda _deps, _cid: [_Stub("does C")])
    assert registry.catalog(deps=None, conversation_id=1) == {"pair": ["does A", "does B"], "single": ["does C"]}
