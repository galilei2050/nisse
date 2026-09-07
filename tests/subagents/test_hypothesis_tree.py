"""HypothesisTree: node-by-node edits + rendering. Pure logic, no deps (add/update/render guards)."""

from app.subagents.hypothesis_tree import HypothesisStatus, HypothesisTree


def test_renders_nested_tree_with_status_and_findings() -> None:
    """Root → branch → leaf render indented, with verdict tag and findings inline."""
    tree = HypothesisTree()
    tree.add("Q0", "why did X happen?", None)
    tree.add("Q1", "is it the numerator?", "Q0")
    tree.add("H1.1", "spend grew", "Q1")
    tree.update("H1.1", HypothesisStatus.VERIFIED, "$2k→$7k (queried)")
    assert tree.render() == (
        "Q0: why did X happen? [untested]\n"
        "  Q1: is it the numerator? [untested]\n"
        "    H1.1: spend grew [verified]\n"
        "      → $2k→$7k (queried)"
    )


def test_add_rejects_duplicate_and_unknown_parent() -> None:
    """A duplicate id or a parent not yet in the tree is refused (loud message, no mutation)."""
    tree = HypothesisTree()
    tree.add("Q0", "root", None)
    assert "already exists" in tree.add("Q0", "again", None)
    assert "isn't in the tree" in tree.add("H1", "leaf", "Q9")
    assert tree.render() == "Q0: root [untested]"  # neither bad add mutated the tree


def test_update_unknown_node_is_refused() -> None:
    """Updating an id that was never added is refused rather than silently creating it."""
    tree = HypothesisTree()
    assert "isn't in the tree" in tree.update("H1", HypothesisStatus.VERIFIED, None)


def test_update_appends_findings_in_order() -> None:
    """Repeated updates accumulate the evidence trail; the latest status wins."""
    tree = HypothesisTree()
    tree.add("H1", "hypothesis", None)
    tree.update("H1", HypothesisStatus.PARTIAL, "first")
    tree.update("H1", HypothesisStatus.VERIFIED, "second")
    assert tree.render() == "H1: hypothesis [verified]\n  → first\n  → second"


def test_empty_tree_renders_hint() -> None:
    """An empty tree renders the start-here hint, not a blank block."""
    assert "add_hypothesis" in HypothesisTree().render()
