"""Core memory: the small always-on block, and the tool that injects + rewrites it.

Mirrors `RecallMemoryTool` (which owns the memory index): one tool both *injects* its content into
the system prompt every turn — via `system_prompt()`, async/per-turn — and *edits* it. Core memory
holds, in three loose sections, the standing context that must shape (almost) every reply: BEHAVIOUR
rules, canonical owner IDENTITY, and the owner's CURRENT FOCUS. Everything topical lives in
`memories` (recalled on demand), never here. The block is size-capped so the agent keeps it lean.
"""

from baski.agents.tool import Tool
from pydantic import BaseModel, Field

from app.prompts.store import PromptStore, PromptType

# Always-on = pure per-turn token cost, so it's hard-capped; the agent must keep only what earns a slot.
_CORE_BUDGET = 2400  # characters

_CORE_HEADER = (
    "CORE MEMORY — your always-on standing context (the only owner-knowledge injected automatically). "
    "Follow the BEHAVIOUR rules; treat CURRENT FOCUS as live context."
)
_CORE_EMPTY = (
    "(empty — when the owner states a standing behaviour rule, an identity fact that shapes most "
    "turns, or a current focus/goal, save it here with update_core_memory.)"
)


class CoreMemoryTool(Tool):
    """Maintain the always-on core-memory block injected into the system every turn. Lifecycle: per-conversation."""

    name = "update_core_memory"
    one_line = "Edit your always-on core memory (behaviour rules, owner identity, current focus)"
    description = (
        "Rewrite your CORE MEMORY (the always-on block). Sections: BEHAVIOUR (how to act/speak/address "
        "the owner); ABOUT THE OWNER (canonical identity that silently shapes most turns — timezone/"
        "language/name; store the operational value, e.g. timezone not 'lives in X'); CURRENT FOCUS "
        "(transient active goals; overwrite/clear as they change). "
        "ROUTING: include something ONLY if ignoring it would make you wrong on a turn that never mentions "
        "it; if it matters only once its topic comes up, use remember instead. A memory is NOT injected "
        "and does NOT substitute for core memory — never skip writing here because a similar memory "
        "exists, and don't search memory first. "
        "Pass the FULL updated text (overwrites wholesale; you always see the current content) — keep "
        "existing lines, amend. Hard-capped: if full, drop the least-relevant line (it can go to "
        "remember). Keep tight — costs tokens every turn."
    )

    class Input(BaseModel):
        """Argument for overwriting the core-memory block."""

        content: str = Field(description="The full core-memory text; replaces the previous version wholesale")

    def __init__(self, store: PromptStore) -> None:
        """Hold the prompt store core memory is read from and written to."""
        self._store = store

    async def execute(self, *, content: str) -> str:
        """Overwrite core memory, rejecting content over the size cap so the block stays lean."""
        if len(content) > _CORE_BUDGET:
            return (
                f"Too long: {len(content)} chars > {_CORE_BUDGET} cap. Core memory must stay small — keep "
                f"only what shapes most turns and move the rest to remember, then resend."
            )
        await self._store.set(PromptType.CORE_MEMORY, content)
        return "Core memory updated."

    async def system_prompt(self) -> str:
        """The core-memory block, read live and injected into the system prompt every turn."""
        content = await self._store.get(PromptType.CORE_MEMORY)
        return f"{_CORE_HEADER}\n\n{content or _CORE_EMPTY}"
