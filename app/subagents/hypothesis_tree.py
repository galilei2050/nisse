"""Hypothesis tree: the researcher's living investigation record, edited node-by-node.

The tree is the methodology's core artifact (`docs/research-subagent.md`): a root question, candidate
branches, and falsifiable leaves whose verdict the researcher sets the moment evidence arrives — never
leaving a leaf `[untested]` once a retrieval result is in. It's shown back to the researcher every
turn, which keeps it pinned as retrieval results fill and truncate the run's context.

Edited with GRANULAR tools (`add_hypothesis` / `update_hypothesis`), not a wholesale rewrite: touching
one node at a time mirrors the codebase's `list_edit`/core-memory idiom, costs a few tokens per edit
instead of resending the whole tree, and can't drop a node by accident. State is ephemeral,
in-instance — one shared `HypothesisTree` per `SubagentTool.execute` run (one investigation), gone
after it. No Mongo, no conversation scope.
"""

from enum import StrEnum

from anthropic.types import MessageParam, TextBlockParam
from baski.agents.tool import Tool
from pydantic import BaseModel, Field

_HEADER = (
    "HYPOTHESIS TREE — your living investigation record, shown every turn. Lay it out BEFORE searching "
    "(add_hypothesis: root question → branches → falsifiable leaves); set each leaf's verdict the moment "
    "its evidence is in (update_hypothesis)."
)
_EMPTY = "(empty — start by adding the root question and its candidate branches with add_hypothesis.)"


class HypothesisStatus(StrEnum):
    """A leaf's verdict — set the moment its evidence is in (methodology: never stay untested)."""

    UNTESTED = "untested"
    VERIFIED = "verified"
    FALSIFIED = "falsified"
    NOT_A_FACTOR = "not a factor"  # mechanism real but too small to explain the effect
    PARTIAL = "partial"  # real, but explains only part of the gap
    OPEN = "open"  # data insufficient to decide — note what would close it


class _Node(BaseModel):
    """One tree node: a question/branch or a falsifiable hypothesis, with its verdict and evidence."""

    node_id: str
    text: str
    parent: str | None
    status: HypothesisStatus
    findings: list[str]


class HypothesisTree:
    """The shared, ephemeral tree state for one investigation. Lifecycle: short-lived (one run)."""

    def __init__(self) -> None:
        """Start empty; nodes are kept in insertion order (dict) for stable rendering."""
        self._nodes: dict[str, _Node] = {}

    def add(self, node_id: str, text: str, parent: str | None) -> str:
        """Add a node as [untested]; reject a duplicate id or a parent that isn't in the tree yet."""
        if node_id in self._nodes:
            return f"'{node_id}' already exists — change it with update_hypothesis."
        if parent is not None and parent not in self._nodes:
            return f"parent '{parent}' isn't in the tree — add it first, or omit parent for a root node."
        self._nodes[node_id] = _Node(
            node_id=node_id, text=text, parent=parent, status=HypothesisStatus.UNTESTED, findings=[]
        )
        return f"Added {node_id} [{HypothesisStatus.UNTESTED}]."

    def update(self, node_id: str, status: HypothesisStatus, finding: str | None) -> str:
        """Set a node's verdict and append its key finding; reject an unknown id."""
        node = self._nodes.get(node_id)
        if node is None:
            return f"'{node_id}' isn't in the tree — add it first with add_hypothesis."
        node.status = status
        if finding:
            node.findings.append(finding)
        return f"Updated {node_id} → [{status}]."

    def render(self) -> str:
        """The whole tree as indented markdown (root → branches → leaves, findings inline)."""
        if not self._nodes:
            return _EMPTY
        lines: list[str] = []
        self._render_children(None, 0, lines)
        return "\n".join(lines)

    def _render_children(self, parent: str | None, depth: int, lines: list[str]) -> None:
        """Depth-first render of every node whose parent is `parent` (add() guarantees no orphans)."""
        indent = "  " * depth
        for node in self._nodes.values():
            if node.parent == parent:
                lines.append(f"{indent}{node.node_id}: {node.text} [{node.status}]")
                lines.extend(f"{indent}  → {finding}" for finding in node.findings)
                self._render_children(node.node_id, depth + 1, lines)


class AddHypothesisTool(Tool):
    """Add one node to the shared hypothesis tree. Lifecycle: short-lived (one investigation)."""

    name = "add_hypothesis"
    one_line = "Add one node to your hypothesis tree (a question/branch or a falsifiable leaf)"
    description = (
        "Add ONE node to your hypothesis tree. Lay the tree out BEFORE gathering evidence: the root "
        "question, its candidate branches, then falsifiable leaves — each testable against what "
        "retrieval returns. Every 'could be A or B' becomes two leaves you both test, never a question "
        "back to the caller. New nodes start [untested]; record verdicts with update_hypothesis. You "
        "see the whole tree every turn."
    )

    class Input(BaseModel):
        """One node to add to the tree."""

        node_id: str = Field(description="short id, e.g. Q0 (root question), Q1 (branch), H1.1 (leaf)")
        text: str = Field(description="the question or the falsifiable hypothesis, one line")
        parent: str | None = Field(default=None, description="id of the parent node; omit for a root")

    def __init__(self, tree: HypothesisTree) -> None:
        """Share the one tree instance with the other tree tool for this investigation."""
        self._tree = tree

    async def execute(self, *, node_id: str, text: str, parent: str | None = None) -> str:
        """Add the node; return the outcome (or why it was rejected) for the model to self-correct."""
        return self._tree.add(node_id, text, parent)


class UpdateHypothesisTool(Tool):
    """Record a verdict on one node, and inject the whole tree every turn. Lifecycle: short-lived."""

    name = "update_hypothesis"
    one_line = "Record the verdict on one hypothesis (set its status the moment evidence is in)"
    description = (
        "Record the verdict on ONE hypothesis the moment its evidence is in — never leave a leaf "
        "[untested] once retrieval has answered it. status is one of: untested, verified, falsified, "
        "'not a factor' (real but too small to matter), partial (explains only part), open (data "
        "insufficient — say what would close it). Put the key number or quote in `finding`. You see "
        "the whole tree every turn."
    )

    class Input(BaseModel):
        """The verdict on one existing node."""

        node_id: str = Field(description="id of the node to update (must already be in the tree)")
        status: HypothesisStatus = Field(description="the verdict for this node")
        finding: str | None = Field(default=None, description="the key evidence/number/quote that set it")

    def __init__(self, tree: HypothesisTree) -> None:
        """Share the one tree instance; this tool also injects the tree into the prompt each turn."""
        self._tree = tree

    async def execute(self, *, node_id: str, status: HypothesisStatus, finding: str | None = None) -> str:
        """Set the verdict; return the outcome (or why it was rejected) for the model to self-correct."""
        return self._tree.update(node_id, status, finding)

    async def system_prompt(self) -> str:
        """The methodology, which never changes — the tree itself rides `user_message` (see there)."""
        return _HEADER

    async def user_message(self) -> MessageParam:
        """Inject the current tree every turn (once — this is the single injection point of the pair).

        Here and not in `system_prompt` because the system block is the FIRST cached block: every edit
        to the tree changed it, and the tools schema, the system prompt and the whole transcript
        behind it were thrown away and re-written. Measured on production traces: a turn following
        `update_hypothesis` read back 8% of its previous context against 97-100% after an ordinary
        tool, and wrote 28.7k tokens against 2.5k. baski orders the volatile blocks after the cached
        prefix (`Agent._build_messages`), so from here the tree reaches the model every turn exactly
        as before — the list and memory indexes are injected the same way for the same reason.
        """
        return MessageParam(role="user", content=[TextBlockParam(type="text", text=self._tree.render())])


def build_hypothesis_tree_tools() -> list[Tool]:
    """A fresh shared tree + the granular add/update tools over it, for one investigation."""
    tree = HypothesisTree()
    return [AddHypothesisTool(tree), UpdateHypothesisTool(tree)]
