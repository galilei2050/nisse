"""Hypothesis tree: the researcher's living investigation record, injected every turn.

Same injection shape as core memory (`prompts/tools.py`) — one tool that both *injects* the current
tree into the system prompt every turn (`system_prompt()`, async/per-turn) and *rewrites* it
(`execute`). Two deliberate differences: the tree is **ephemeral in-instance state**, not a Mongo
store — one instance per `SubagentTool.execute` run (= one investigation), gone after it; and it's
rewritten whole each call (single writer, coherent hierarchy) rather than patched line-by-line like
core memory. Keeping the tree pinned in the system prompt is what holds it stable as retrieval
results fill and truncate the run's context.
"""

from baski.agents.tool import Tool
from pydantic import BaseModel, Field

# The tree costs tokens every turn (injected + rewritten), so it's hard-capped; on overflow the
# researcher must prune (collapse resolved branches to their status) rather than let it grow.
_TREE_BUDGET = 6000  # characters

_TREE_HEADER = (
    "HYPOTHESIS TREE — your living investigation record, shown back to you every turn. Update it "
    "with hypothesis_tree after each retrieval result; never leave a leaf [untested] once you have "
    "evidence."
)
_TREE_EMPTY = (
    "(empty — before gathering any evidence, lay out the tree with hypothesis_tree: the root "
    "question, candidate branches, and falsifiable leaves each tagged [untested].)"
)


class HypothesisTreeTool(Tool):
    """Maintain the researcher's hypothesis tree, injected into its system prompt every turn.

    Lifecycle: short-lived — one instance per investigation (built fresh inside each
    `SubagentTool.execute` run); the tree lives in the instance, not in any store.
    """

    name = "hypothesis_tree"
    one_line = "Maintain your hypothesis tree — the living record of the investigation"
    description = (
        "Maintain your hypothesis tree — the living record of the investigation, shown back to you "
        "every turn. Pass the FULL updated tree each call; it replaces the previous one whole (unlike "
        "line-edited core memory, a hierarchical tree is clearer rewritten than patched). Lay it out "
        "BEFORE gathering evidence: root question → candidate branches → falsifiable leaves, each "
        "tagged [untested]. After each retrieval result, update the leaf's status: [VERIFIED] "
        "[FALSIFIED] [NOT A FACTOR] [PARTIAL] [OPEN] (data missing — note what would close it). Keep "
        "key numbers/quotes inline. Keep it lean — it costs tokens every turn."
    )

    class Input(BaseModel):
        """The full hypothesis tree to store — rewritten whole each call."""

        tree: str = Field(
            description="The complete markdown hypothesis tree (root → branches → status-tagged leaves). "
            "Pass the whole tree every time; it replaces the previous one."
        )

    def __init__(self) -> None:
        """Start with an empty tree; it lives in this instance for the one investigation."""
        self._tree = ""

    async def execute(self, *, tree: str) -> str:
        """Replace the stored tree with the full new one; reject if it exceeds the cap."""
        if len(tree) > _TREE_BUDGET:
            return (
                f"Too long: {len(tree)} chars > {_TREE_BUDGET} cap. Prune resolved branches to their "
                f"status (drop the reasoning once a leaf is VERIFIED/FALSIFIED) and pass the tree again."
            )
        self._tree = tree
        return "Hypothesis tree updated."

    async def system_prompt(self) -> str:
        """The current tree, read live and injected into the system prompt every turn."""
        return f"{_TREE_HEADER}\n\n{self._tree or _TREE_EMPTY}"
