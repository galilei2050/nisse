"""The agent's LIST tools — TWO of them, on purpose: list_edit (mutate) + list_show (read).

A list is the ARTIFACT tier — a mutable named collection (shopping, todo, watchlist…), NOT long-term
memory. The four CRUD verbs collapse into one mutation tool so the always-on tool schemas stay small
(every tool's schema rides the context window on every turn): `list_edit` does add + remove + clear
in one call. `list_show` is kept separate so a pure read never carries a side effect, and it injects
the always-present index of list names + counts via `user_message()`.
"""

from anthropic.types import MessageParam, TextBlockParam
from baski.agents.tool import Tool
from pydantic import BaseModel, Field

from app.lists.store import ItemList, ListStore
from app.shared import CoreDeps

_LIST_GUIDANCE = (
    "LISTS = collections the owner adds to and crosses off (shopping, todo, or a growing log like the "
    "owner's contradictions). Use list_edit/list_show, not long-term memory. Remove by the exact item "
    "or a distinctive fragment (dropped only if it uniquely matches; else you're asked to be specific). "
    "Whole items only — to revise a word inside one, use a memory body. Reuse one short lower-case name."
)

_INDEX_HEADER = (
    "YOUR LISTS (artifacts — mutable, not memory). Each line: name (item count). Call list_show(name) "
    "to see items before answering about a list; list_edit to add/remove/clear."
)


def _summary(lst: ItemList) -> str:
    """One-line confirmation: list name, count, and items (or empty)."""
    return f"'{lst.name}' ({len(lst.items)} items): {', '.join(lst.items) if lst.items else '(empty)'}"


class ListEditTool(Tool):
    """Add/remove items or clear a named list in one call. Lifecycle: per-conversation (in its toolset)."""

    name = "list_edit"
    one_line = "LIST: add and/or remove items on a named list, or clear it (creates it on first add)"
    description = (
        "Add and/or remove items on a named list (creates it on first add). Remove by the exact item "
        "or a distinctive fragment of a longer one (unique match only). clear=true deletes the list."
    )

    class Input(BaseModel):
        """Arguments for one list mutation — add and/or remove, or clear."""

        name: str = Field(description="Short list name, reused to edit the same list (e.g. 'shopping')")
        add: list[str] = Field(default_factory=list, description="Items to add; duplicates already present are skipped")
        remove: list[str] = Field(
            default_factory=list,
            description="Items to remove: the exact item, or a short distinctive fragment of a longer item "
            "(removed only when it uniquely matches one item)",
        )
        clear: bool = Field(default=False, description="Delete the entire list (ignores add/remove)")

    def __init__(self, store: ListStore) -> None:
        """Hold the store every edit writes to."""
        self._store = store

    async def execute(
        self, *, name: str, add: list[str] | None = None, remove: list[str] | None = None, clear: bool = False
    ) -> str:
        """Clear, or apply add then remove, and confirm the resulting list (+ any remove notes)."""
        if clear:
            return f"Cleared list '{name}'." if await self._store.clear(name) else f"No list '{name}'."
        current: ItemList | None = None
        notes: list[str] = []
        if add:
            current = await self._store.add(name, add)
        if remove:
            outcome = await self._store.remove(name, remove)
            current = outcome.updated
            if outcome.ambiguous:
                notes.append(f"ambiguous (matches several — be more specific): {', '.join(outcome.ambiguous)}")
            if outcome.missing:
                notes.append(f"not on the list: {', '.join(outcome.missing)}")
        if current is None:  # no add/remove given, or remove on a missing list
            current = await self._store.get(name)
        if current is None:
            return f"No list '{name}'."
        return _summary(current) + (f"\n({'; '.join(notes)})" if notes else "")

    async def system_prompt(self) -> str:
        """The list-vs-memory routing policy (the load-bearing rule against the duplication bug)."""
        return _LIST_GUIDANCE


class ListShowTool(Tool):
    """Read one list's items + inject the list index. Lifecycle: per-conversation (in its toolset)."""

    name = "list_show"
    one_line = "LIST: show the items of a named list"
    description = "Show all items currently on a named list."

    class Input(BaseModel):
        """Argument for showing one list."""

        name: str = Field(description="List name from the index")

    def __init__(self, store: ListStore) -> None:
        """Hold the store; the index is read fresh on every turn (never cached)."""
        self._store = store

    async def execute(self, *, name: str) -> str:
        """Return the list's items, or a not-found note."""
        result = await self._store.get(name)
        if result is None:
            return f"No list '{name}'."
        if not result.items:
            return f"'{result.name}' is empty."
        return f"'{result.name}' ({len(result.items)} items):\n" + "\n".join(f"- {i}" for i in result.items)

    async def user_message(self) -> MessageParam | None:
        """The always-injected list index (names + counts), read live so mid-run edits show up."""
        lists = await self._store.all()
        if not lists:
            return None
        lines = [_INDEX_HEADER]
        lines.extend(f"- {lst.name} ({len(lst.items)} items)" for lst in lists)
        return MessageParam(role="user", content=[TextBlockParam(type="text", text="\n".join(lines))])


def list_tools(deps: CoreDeps, conversation_id: int) -> list[Tool]:
    """Named lists (artifact tier) — one store scoped to the chat, its edit/show tools."""
    store = ListStore(deps.database, conversation_id=conversation_id)
    return [ListEditTool(store), ListShowTool(store)]
