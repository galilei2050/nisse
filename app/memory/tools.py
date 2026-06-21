"""The agent's long-term memory tools: remember, read_memory, forget.

The read tool also injects the always-present memory index via `user_message()` —
one pointer line per memory; bodies are loaded on demand by public_id.
"""

from typing import TypedDict

from anthropic.types import MessageParam, TextBlockParam
from baski.agents.tool import Tool
from pydantic import BaseModel, Field

from app.memory.store import MemoryCategory, MemorySource, MemoryStore, SourceKind

# Index groups follow the category's own declaration order — one source of truth, so a new
# category can never silently drop out of the index.
_CATEGORY_ORDER: tuple[MemoryCategory, ...] = tuple(MemoryCategory)


class _SourceArg(TypedDict):
    """The dict shape `remember`'s nested `source` arrives as after schema validation."""

    kind: SourceKind
    ref: str | None


_REMEMBER_GUIDANCE = (
    "Save only durable owner knowledge worth recalling in a later unrelated conversation: stable facts, "
    "stated preferences, dated events. Tool output, research, task steps → store_memory, not here; "
    "unsure → skip (missed fact = one question; saved scrap pollutes every turn). Check index first; to "
    "correct, pass that public_id here to overwrite whole (or edit_memory for a small change to a long "
    "body) — never duplicate. category: fact = stable truth; event = dated. How the owner wants you to "
    "behave or address them → update_preferences, not here. "
    'SKIP e.g. "debugging deploy script", "flight BCN→LIS €78".\n'
    "Context auto-trims: pure tool turns drop after each reply, old turns trimmed as window fills — so a "
    "durable fact from a tool result is gone next turn unless you remember it here now."
)

_INDEX_HEADER = (
    "YOUR LONG-TERM MEMORY (grouped by category). Each line is a pointer: [public_id] source · date — "
    "title; date = last updated, an old one may be stale. When a title is relevant, call read_memory("
    "public_id) to load the body before answering — don't answer from assumption when a relevant memory "
    "exists, and read only what's relevant. Don't re-save what's already here."
)

_FORGET_GUIDANCE = (
    "forget(public_id) only to drop a memory for good — fact no longer holds, nothing replaces it. If "
    "the owner changes their mind or refines a fact, correct in place instead: remember(public_id, …) to "
    "overwrite whole, or edit_memory for part of a long body — never forget-then-re-add, never leave both "
    "stale and new. Don't forget just because it's old or off-topic."
)

_EDIT_GUIDANCE = (
    "edit_memory(public_id, old, new) patches a body in place — fix/extend a long body without a full "
    "rewrite. Replace: old = exact current text, new = replacement (new empty deletes it). Append: old "
    "empty, new = line to add. If old doesn't match verbatim, nothing changes and you get the current "
    "body back — retry against it. For a small record, or to change title/category, use "
    "remember(public_id, …) to overwrite whole instead."
)


class RememberTool(Tool):
    """Persist durable knowledge to long-term memory. Lifecycle: per-conversation (in its toolset)."""

    name = "remember"
    one_line = "Save a durable fact/preference/event about the owner"
    description = (
        "Persist knowledge to long-term memory; survives all future conversations. "
        "Pass public_id to overwrite a memory whole, else create."
    )

    class Input(BaseModel):
        """Arguments for storing one long-term memory."""

        public_id: str | None = Field(
            default=None,
            description="Omit to create; pass an existing public_id to overwrite that memory whole",
        )
        title: str = Field(description="Short title shown in the always-visible index")
        category: MemoryCategory = Field(description="fact, preference, or event")
        source: MemorySource = Field(description="kind ∈ user/external/agent; ref = url/name, required when external")
        body: str = Field(description="Full memory text, fetched on demand by public_id")

    def __init__(self, store: MemoryStore) -> None:
        """Hold the store every save writes to."""
        self._store = store

    async def execute(  # noqa: PLR0913 — the remember fields plus an optional public_id to overwrite
        self, *, title: str, category: MemoryCategory, source: _SourceArg, body: str, public_id: str | None = None
    ) -> str:
        """Create a memory, or overwrite an existing one when public_id is given; confirm the live id."""
        src = MemorySource.model_validate(source)
        if public_id is None:
            memory = await self._store.add(title=title, category=category, source=src, body=body)
            return f"Saved memory {memory.public_id}."
        memory = await self._store.overwrite(public_id, title=title, category=category, source=src, body=body)
        if memory.public_id == public_id:
            return f"Updated memory {memory.public_id}."
        return f"Saved new memory {memory.public_id}."  # the id was gone; a fresh one was created

    async def system_prompt(self) -> str:
        """The long-term save policy."""
        return _REMEMBER_GUIDANCE


class RecallMemoryTool(Tool):
    """Read a remembered item body + inject the memory index. Lifecycle: per-conversation (in its toolset)."""

    name = "read_memory"
    one_line = "Read the full body of a remembered item by its public_id"
    description = "Load the full body of a memory listed in the index."

    class Input(BaseModel):
        """Argument for reading one memory body."""

        public_id: str = Field(description="public_id from the index (shown in [brackets])")

    def __init__(self, store: MemoryStore) -> None:
        """Hold the store; the index is read fresh from it on every turn (never cached)."""
        self._store = store

    async def execute(self, *, public_id: str) -> str:
        """Return the memory body (plus its source link when external), or a not-found note."""
        memory = await self._store.get(public_id)
        if memory is None:
            return f"No memory {public_id}."
        return f"{memory.body}\n\nsource: {memory.source.ref}" if memory.source.ref else memory.body

    async def user_message(self) -> MessageParam | None:
        """The always-injected index, read live from the store so mid-run writes show up."""
        memories = await self._store.list()
        if not memories:
            return None
        lines = [_INDEX_HEADER]
        for category in _CATEGORY_ORDER:
            group = [m for m in memories if m.category == category]
            if not group:
                continue
            lines.append(f"\n{category.upper()}")
            # kind only here (cheap provenance); the external ref/url is detail, returned by read_memory.
            lines.extend(
                f"- [{m.public_id}] {m.source.kind} · {m.updated_at.strftime('%Y-%m-%d')} — {m.title}" for m in group
            )
        return MessageParam(role="user", content=[TextBlockParam(type="text", text="\n".join(lines))])


class EditMemoryTool(Tool):
    """Patch a memory's body in place — replace a fragment, or append. Lifecycle: per-conversation (in its toolset)."""

    name = "edit_memory"
    one_line = "Edit a memory's body in place: replace a fragment, or append (empty old)"
    description = "Patch a memory's body in place: replace `old` with `new`, or append `new` when `old` is empty."

    class Input(BaseModel):
        """Arguments for an in-place body patch."""

        public_id: str = Field(description="public_id from the index")
        old: str = Field(description='Exact current text to replace; empty ("") appends `new` instead')
        new: str = Field(description="Replacement text, or line to append when `old` empty. Empty deletes `old`.")

    def __init__(self, store: MemoryStore) -> None:
        """Hold the store the patch reads and writes."""
        self._store = store

    async def execute(self, *, public_id: str, old: str, new: str) -> str:
        """Replace `old` with `new` in the body, or append `new` when `old` is empty; confirm or report."""
        memory = await self._store.get(public_id)
        if memory is None:
            return f"No memory {public_id}."
        if old == "":
            body = f"{memory.body}\n{new}" if memory.body else new
        elif old in memory.body:
            body = memory.body.replace(old, new, 1)
        else:
            return (
                f"`old` not found verbatim in {public_id} — nothing changed. "
                f"Edit against the current body:\n{memory.body}"
            )
        await self._store.set_body(public_id, body=body)
        return f"Edited memory {public_id}."

    async def system_prompt(self) -> str:
        """When and how to patch a body in place."""
        return _EDIT_GUIDANCE


class ForgetTool(Tool):
    """Delete a long-term memory that has gone stale or wrong. Lifecycle: per-conversation (in its toolset)."""

    name = "forget"
    one_line = "Delete a memory that is stale or wrong"
    description = "Remove a long-term memory that no longer holds."

    class Input(BaseModel):
        """Argument for deleting one memory."""

        public_id: str = Field(description="public_id from the index")

    def __init__(self, store: MemoryStore) -> None:
        """Hold the store every delete acts on."""
        self._store = store

    async def execute(self, *, public_id: str) -> str:
        """Soft-delete the memory and confirm."""
        return f"Forgot memory {public_id}." if await self._store.soft_delete(public_id) else f"No memory {public_id}."

    async def system_prompt(self) -> str:
        """When to forget."""
        return _FORGET_GUIDANCE
