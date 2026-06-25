"""Core memory: the small always-on block, and the tool that injects + edits it in place.

Mirrors `EditMemoryTool` (`recall_edit`): one tool both *injects* its content into the system prompt
every turn — via `system_prompt()`, async/per-turn — and *patches* it (append / replace / remove a
fragment), so the agent amends a single line instead of resending the whole block every time. Core
memory holds, in three loose sections, the standing context that must shape (almost) every reply:
BEHAVIOUR rules, canonical owner IDENTITY, and the owner's CURRENT FOCUS. Everything topical lives in
`memories` (recalled on demand), never here. The block is size-capped so the agent keeps it lean.
"""

from baski.agents.tool import Tool
from pydantic import BaseModel, Field

from app.prompts.store import PromptStore, PromptType

# Always-on = pure per-turn token cost, so it's hard-capped; the agent must keep only what earns a slot.
# ~3000 chars ≈ 750 tokens/turn. Raised from 2400 once patch-in-place removed the resend-churn and real
# traffic kept the block pinned at the old cap (more standing rules than fit → rules got dropped).
_CORE_BUDGET = 3000  # characters

_CORE_HEADER = (
    "CORE MEMORY — your always-on standing context (the only owner-knowledge injected automatically). "
    "Follow the BEHAVIOUR rules; treat CURRENT FOCUS as live context."
)
_CORE_EMPTY = (
    "(empty — when the owner states a standing behaviour rule, an identity fact that shapes most "
    "turns, or a current focus/goal, add it here with update_core_memory.)"
)


class CoreMemoryTool(Tool):
    """Maintain the always-on core-memory block injected into the system every turn. Lifecycle: per-conversation."""

    name = "update_core_memory"
    one_line = "Edit your always-on core memory in place — append a line (empty old), or replace/remove a fragment"
    description = (
        "Patch your CORE MEMORY (the always-on block) in place: append `new` when `old` is empty, replace "
        "`old` with `new`, or remove `old` when `new` is empty. You see the current block every turn — "
        "amend a line, don't resend the whole thing. Sections: BEHAVIOUR (how to act/speak/address the "
        "owner); ABOUT THE OWNER (canonical identity that silently shapes most turns — timezone/language/"
        "name; store the operational value, e.g. timezone not 'lives in X'); CURRENT FOCUS (transient "
        "active goals). "
        "ROUTING: add something ONLY if ignoring it would make you wrong on a turn that never mentions it; "
        "if it matters only once its topic comes up, use recall_save (LONG-TERM MEMORY) instead. A "
        "long-term memory is NOT injected and does NOT substitute for CORE MEMORY — never skip writing here "
        "because a similar memory exists, and don't search memory first. CORE is owner-knowledge only — how "
        "YOU operate (context pruning, tool workflows) is NOT core memory. Keep tight — costs tokens every turn."
    )

    class Input(BaseModel):
        """Arguments for an in-place core-memory patch (mirrors recall_edit, but there is one block, so no id)."""

        old: str = Field(description='Exact current text to replace/remove; empty ("") appends `new`')
        new: str = Field(description="Replacement text, or line to append when `old` empty. Empty removes `old`.")

    def __init__(self, store: PromptStore) -> None:
        """Hold the prompt store core memory is read from and written to."""
        self._store = store

    async def execute(self, *, old: str, new: str) -> str:
        """Append/replace/remove a fragment in place; reject if the result would exceed the size cap."""
        content = await self._store.get(PromptType.CORE_MEMORY) or ""
        if old == "":
            updated = f"{content}\n{new}" if content else new
        elif old in content:
            updated = content.replace(old, new, 1)
        else:
            return f"`old` not found verbatim — nothing changed. Patch against the current core memory:\n{content}"
        if len(updated) > _CORE_BUDGET:
            return (
                f"Too long: {len(updated)} chars > {_CORE_BUDGET} cap. Core memory must stay small — replace "
                f"or remove a less-relevant line instead of appending, or move it to recall_save."
            )
        await self._store.set(PromptType.CORE_MEMORY, updated)
        return "Core memory updated."

    async def system_prompt(self) -> str:
        """The core-memory block, read live and injected into the system prompt every turn."""
        content = await self._store.get(PromptType.CORE_MEMORY)
        return f"{_CORE_HEADER}\n\n{content or _CORE_EMPTY}"
