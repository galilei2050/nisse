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
    "Save only durable owner knowledge worth recalling in a later, unrelated conversation — stable "
    "facts, stated preferences, dated events. Tool output, research, and task steps go to store_memory, "
    "not here; when unsure, skip (a missed fact costs one question; a saved scrap pollutes every turn). "
    "Check the index first — update or forget a stale entry, don't duplicate. category: fact = stable "
    'truth; preference = how they like things; event = something dated. SKIP e.g. "debugging the deploy '
    'script", "flight BCN→LIS €78".'
)

_INDEX_HEADER = (
    "YOUR LONG-TERM MEMORY (grouped by category). Each line is a pointer: [public_id] source · date — "
    "title; date = when learned, an old one may be stale. When a title is relevant, call read_memory("
    "public_id) to load the body before answering — don't answer from assumption when a relevant memory "
    "exists, and read only what's relevant. Don't re-save what's already here."
)

_FORGET_GUIDANCE = (
    "Call forget(public_id) when a memory is wrong or superseded — the owner changed their mind or a "
    "newer memory replaces it. On a contradiction, forget the stale one (or re-remember the correction) "
    "— don't leave both. Don't forget just because it's old or off-topic."
)


class RememberTool(Tool):
    """Persist durable knowledge to long-term memory. Lifecycle: per-conversation (in its toolset)."""

    name = "remember"
    one_line = "Save a durable fact/preference/event about the owner"
    description = "Persist knowledge to long-term memory; it survives across every future conversation."

    class Input(BaseModel):
        """Arguments for storing one long-term memory."""

        title: str = Field(description="Short title shown in the always-visible memory index")
        category: MemoryCategory = Field(description="fact, preference, or event")
        source: MemorySource = Field(
            description="where it came from: kind ∈ user/external/agent; ref = url or name when external"
        )
        body: str = Field(description="The full memory text, read on demand by public_id")

    def __init__(self, store: MemoryStore) -> None:
        """Hold the store every save writes to."""
        self._store = store

    async def execute(self, *, title: str, category: MemoryCategory, source: _SourceArg, body: str) -> str:
        """Write the memory and confirm with its public id."""
        memory = await self._store.add(
            title=title, category=category, source=MemorySource.model_validate(source), body=body
        )
        return f"Saved memory {memory.public_id}."

    def system_prompt(self) -> str:
        """The long-term save policy."""
        return _REMEMBER_GUIDANCE


class RecallMemoryTool(Tool):
    """Read a remembered item body + inject the memory index. Lifecycle: per-conversation (in its toolset)."""

    name = "read_memory"
    one_line = "Read the full body of a remembered item by its public_id"
    description = "Load the full body of a long-term memory listed in the memory index."

    class Input(BaseModel):
        """Argument for reading one memory body."""

        public_id: str = Field(description="The public_id shown in [brackets] in the memory index")

    def __init__(self, store: MemoryStore) -> None:
        """Hold the store; the index is read fresh from it on every turn (never cached)."""
        self._store = store

    async def execute(self, *, public_id: str) -> str:
        """Return the memory body, or a not-found note."""
        memory = await self._store.get(public_id)
        return memory.body if memory else f"No memory {public_id}."

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
            for m in group:
                src = m.source.kind if m.source.ref is None else f"{m.source.kind}:{m.source.ref}"
                lines.append(f"- [{m.public_id}] {src} · {m.created_at.strftime('%Y-%m-%d')} — {m.title}")
        return MessageParam(role="user", content=[TextBlockParam(type="text", text="\n".join(lines))])


class ForgetTool(Tool):
    """Delete a long-term memory that has gone stale or wrong. Lifecycle: per-conversation (in its toolset)."""

    name = "forget"
    one_line = "Delete a memory that is stale or wrong"
    description = "Remove a long-term memory that no longer holds."

    class Input(BaseModel):
        """Argument for deleting one memory."""

        public_id: str = Field(description="The public_id of the memory to delete, from the index")

    def __init__(self, store: MemoryStore) -> None:
        """Hold the store every delete acts on."""
        self._store = store

    async def execute(self, *, public_id: str) -> str:
        """Soft-delete the memory and confirm."""
        return f"Forgot memory {public_id}." if await self._store.soft_delete(public_id) else f"No memory {public_id}."

    def system_prompt(self) -> str:
        """When to forget."""
        return _FORGET_GUIDANCE
