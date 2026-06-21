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

_LIST_GUIDANCE = (
    "LISTS are mutable named collections the owner keeps and edits — shopping, todo, watchlist, "
    "packing. Use list_edit/list_show for ANYTHING the owner calls a list or adds items to over time. "
    "Do NOT put a list in long-term memory (recall_save) — a list is an artifact you add to and cross "
    'off, not a durable fact. "Add milk" → list_edit(name="shopping", add=["milk"]); "got the eggs" → '
    'list_edit(name="shopping", remove=["eggs"]); "what\'s on my list" → list_show. Pick a short '
    "lower-case name and reuse it (the same name always edits the same list)."
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
        "Edit a named list (e.g. shopping, todo): add and/or remove items in one call; creates the "
        "list on first add. Set clear=true to delete the whole list instead. Same name always edits "
        "the same list."
    )

    class Input(BaseModel):
        """Arguments for one list mutation — add and/or remove, or clear."""

        name: str = Field(description="Short list name, reused to edit the same list (e.g. 'shopping')")
        add: list[str] = Field(default_factory=list, description="Items to add; duplicates already present are skipped")
        remove: list[str] = Field(default_factory=list, description="Items to remove (matched case-insensitively)")
        clear: bool = Field(default=False, description="Delete the entire list (ignores add/remove)")

    def __init__(self, store: ListStore) -> None:
        """Hold the store every edit writes to."""
        self._store = store

    async def execute(
        self, *, name: str, add: list[str] | None = None, remove: list[str] | None = None, clear: bool = False
    ) -> str:
        """Clear, or apply add then remove, and confirm the resulting list."""
        if clear:
            return f"Cleared list '{name}'." if await self._store.clear(name) else f"No list '{name}'."
        result: ItemList | None = None
        if add:
            result = await self._store.add(name, add)
        if remove:
            result = await self._store.remove(name, remove)
        if result is None:  # no add/remove given, or remove on a missing list
            result = await self._store.get(name)
        return _summary(result) if result else f"No list '{name}'."

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
