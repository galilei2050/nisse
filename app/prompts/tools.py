"""Core memory: the small always-on block, and the tool that injects + edits it like a list.

One tool both *injects* the block into the system prompt every turn — via `system_prompt()`,
async/per-turn — and *edits it like a list* (`list_edit`-style add/remove of whole lines), so a
standing rule is added, replaced, or removed one line at a time and the block is never overwritten
wholesale (which silently dropped rules). Core memory holds, in three loose sections, the standing
context that must shape (almost) every reply: BEHAVIOUR rules, canonical owner IDENTITY, and the
owner's CURRENT FOCUS. Everything topical lives in `memories` (recalled on demand), never here. The
block is size-capped so the agent keeps it lean.
"""

from typing import NamedTuple

from baski.agents.tool import Tool
from pydantic import BaseModel, Field

from app.prompts.store import PromptStore, PromptType
from app.shared import CoreDeps
from app.shared.text import match_unique
from app.tools.registry import ToolRegistrar

# Always-on = pure per-turn token cost, so it's hard-capped; the agent must keep only what earns a slot.
# ~4500 chars ≈ 1125 tokens/turn. Raised x1.5 from 3000 — real traffic kept the block pinned at the cap,
# so standing rules were still getting dropped for lack of room.
_CORE_BUDGET = 4500  # characters

_CORE_HEADER = (
    "CORE MEMORY — your always-on standing context (the only owner-knowledge injected automatically). "
    "Follow the BEHAVIOUR rules; treat CURRENT FOCUS as live context."
)
_CORE_EMPTY = (
    "(empty — when the owner states a standing behaviour rule, an identity fact that shapes most "
    "turns, or a current focus/goal, add it here with update_core_memory.)"
)


class _RemoveOutcome(NamedTuple):
    """Result of removing lines: the kept lines, and notes for terms that were ambiguous or missing."""

    kept: list[str]
    notes: list[str]


def _remove_lines(lines: list[str], terms: list[str]) -> _RemoveOutcome:
    """Drop lines matched by each term (exact line or unique fragment); report ambiguous/missing terms."""
    candidates = [ln for ln in lines if ln.strip()]
    drop: set[str] = set()
    notes: list[str] = []
    for term in terms:
        hits = match_unique(candidates, term)
        if len(hits) == 1:
            drop.add(hits[0])
        elif len(hits) > 1:
            notes.append(f"ambiguous (matches several — be more specific): {term}")
        else:
            notes.append(f"not in core memory: {term}")
    return _RemoveOutcome([ln for ln in lines if ln not in drop], notes)


def _add_lines(lines: list[str], items: list[str]) -> list[str]:
    """Append items not already present (case-insensitive), preserving order."""
    present = {ln.strip().lower() for ln in lines if ln.strip()}
    for item in items:
        if item.strip() and item.strip().lower() not in present:
            lines.append(item)
            present.add(item.strip().lower())
    return lines


class CoreMemoryTool(Tool):
    """Maintain the always-on core-memory block injected into the system every turn. Lifecycle: per-conversation."""

    name = "update_core_memory"
    one_line = "Edit your always-on core memory like a list — add and/or remove whole lines in one call"
    description = (
        "Edit your CORE MEMORY (the always-on block) like a list: add and/or remove whole lines in one "
        "call. Touch ONLY the lines you name — never rewrite the block wholesale (that silently drops "
        "rules). To replace a rule, remove the old line and add the new one in the same call. Remove by "
        "the exact line or a distinctive fragment of it (dropped only if it uniquely matches; else you're "
        "asked to be more specific). You see the current block every turn — don't add a line already "
        "covered. Sections: BEHAVIOUR (how to act/speak/address the owner); ABOUT THE OWNER (canonical "
        "identity that silently shapes most turns — timezone/language/name; store the operational value, "
        "e.g. timezone not 'lives in X'); CURRENT FOCUS (active goals). "
        "ROUTING: add something ONLY if ignoring it would make you wrong on a turn that never mentions it; "
        "if it matters only once its topic comes up, use recall_save (LONG-TERM MEMORY) instead. A "
        "long-term memory is NOT injected and does NOT substitute for CORE MEMORY — never skip writing here "
        "because a similar memory exists, and don't search memory first. CORE is owner-knowledge only — how "
        "YOU operate (context pruning, tool workflows) is NOT core memory. Keep each line tight — costs tokens "
        "every turn."
    )

    class Input(BaseModel):
        """Arguments for one core-memory edit — add and/or remove whole lines (mirrors list_edit)."""

        add: list[str] = Field(
            default_factory=list,
            description="Lines to add (one concise rule/fact each); duplicates already present are skipped",
        )
        remove: list[str] = Field(
            default_factory=list,
            description="Lines to remove: the exact line, or a short distinctive fragment of a longer one "
            "(removed only when it uniquely matches one line)",
        )

    def __init__(self, store: PromptStore) -> None:
        """Hold the prompt store core memory is read from and written to."""
        self._store = store

    async def execute(self, *, add: list[str] | None = None, remove: list[str] | None = None) -> str:
        """Apply remove then add over the block's lines; reject if the result exceeds the cap. Mirrors list_edit."""
        if not add and not remove:
            return "Nothing to do — pass lines to add and/or remove."
        lines = (await self._store.get(PromptType.CORE_MEMORY) or "").split("\n")
        notes: list[str] = []
        if remove:
            lines, notes = _remove_lines(lines, remove)  # NamedTuple unpacks to (kept, notes)
        if add:
            lines = _add_lines(lines, add)
        updated = "\n".join(lines).strip("\n")
        if len(updated) > _CORE_BUDGET:
            return (
                f"Too long: {len(updated)} chars > {_CORE_BUDGET} cap. Remove a less-relevant line in the "
                f"same call, or move it to recall_save."
            )
        await self._store.set(PromptType.CORE_MEMORY, updated)
        return "Core memory updated." + (f"\n({'; '.join(notes)})" if notes else "")

    async def system_prompt(self) -> str:
        """The core-memory block, read live and injected into the system prompt every turn."""
        content = await self._store.get(PromptType.CORE_MEMORY)
        return f"{_CORE_HEADER}\n\n{content or _CORE_EMPTY}"


def core_memory_tools(deps: CoreDeps, conversation_id: int) -> list[Tool]:
    """The always-on core-memory block editor, over a chat-scoped prompt store."""
    return [CoreMemoryTool(PromptStore(deps.database, conversation_id=conversation_id))]


def register_tools(registrar: ToolRegistrar) -> None:
    """Register the core-memory editor under the name the main agent references."""
    registrar.register("core_memory", core_memory_tools)
