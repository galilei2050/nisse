"""ARTIFACT TIER — named, mutable lists in Mongo (shopping list, todo, watchlist, packing…).

A list is an *artifact*: a mutable collection the owner edits over time, NOT a durable fact or
dated event. It does NOT belong in long-term memory (`app/memory`) — storing a shopping list there
produced duplicated, contradicting copies because memory is meant for inert facts recalled by topic,
not a thing you add to and cross off. One canonical doc per `(conversation_id, name)`; items are
edited in place.
"""

from dataclasses import dataclass, field

from baski.primitives import datetime
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.models import NisseDbModel
from app.shared.mongo import ensure_index
from app.shared.text import match_unique

_COLLECTION = "lists"


def _norm_name(name: str) -> str:
    """Canonical list key — stripped, lower-cased — so 'Shopping' and 'shopping' are one list."""
    return name.strip().lower()


class ItemList(NisseDbModel):
    """One named list of string items, scoped to a conversation. Lifecycle: a mutable data record."""

    conversation_id: int
    name: str  # canonical (normalized) name, unique with conversation_id
    items: list[str]


@dataclass
class RemoveResult:
    """Outcome of a `remove` — which terms hit one item, which were ambiguous, which matched nothing.

    Lets the tool tell the model to retry an ambiguous term with a more specific fragment.
    """

    updated: ItemList | None  # the list after removal, or None if no such live list
    removed: list[str] = field(default_factory=list)  # the items actually dropped
    ambiguous: list[str] = field(default_factory=list)  # terms whose fragment matched >1 item (left intact)
    missing: list[str] = field(default_factory=list)  # terms matching no item


class ListStore:
    """CRUD over the `lists` collection, scoped to one conversation and addressed by list name.

    Lifecycle: per-conversation — built in `_build_list_tools` and held by that chat's tools.
    Items are matched/de-duplicated case-insensitively; the name is normalized so a list is never
    forked by capitalization.
    """

    def __init__(self, database: AsyncDatabase, *, conversation_id: int) -> None:
        """Bind to the lists collection for one conversation; every query is scoped to it."""
        self._collection = database[_COLLECTION]
        self._conversation_id = conversation_id

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """Unique (conversation_id, name) — one canonical list per name per chat. Idempotent."""
        await ensure_index(database[_COLLECTION], [("conversation_id", 1), ("name", 1)], unique=True)

    async def all(self) -> list[ItemList]:
        """Every live list in this conversation, for the always-injected index."""
        query = {"conversation_id": self._conversation_id, "deleted_at": None}
        return [ItemList.model_validate(doc) async for doc in self._collection.find(query)]

    async def get(self, name: str) -> ItemList | None:
        """One live list by name within this conversation, or None."""
        doc = await self._collection.find_one(
            {"conversation_id": self._conversation_id, "name": _norm_name(name), "deleted_at": None}
        )
        return ItemList.model_validate(doc) if doc else None

    async def add(self, name: str, items: list[str]) -> ItemList:
        """Append items to the named list (create it if absent), skipping case-insensitive duplicates.

        Read-modify-write in app code (not `$addToSet`) to keep insertion order + case-folded dedup;
        safe because replies are serialized by the conversation lock. If the named list was cleared
        (soft-deleted), `get` returns None and the upsert REVIVES that doc fresh — re-adding by name
        intentionally starts a new list (the unique (conversation_id, name) index forbids a parallel doc).
        """
        existing = await self.get(name)
        merged = list(existing.items) if existing else []
        seen = {i.lower() for i in merged}
        for item in items:
            if item.strip() and item.lower() not in seen:
                merged.append(item)
                seen.add(item.lower())
        now = datetime.now()
        doc = await self._collection.find_one_and_update(
            {"conversation_id": self._conversation_id, "name": _norm_name(name)},
            {
                "$set": {"items": merged, "deleted_at": None, "updated_at": now},
                "$setOnInsert": {"conversation_id": self._conversation_id, "name": _norm_name(name), "created_at": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return ItemList.model_validate(doc)

    async def remove(self, name: str, terms: list[str]) -> RemoveResult:
        """Remove items matched by each term: exact (case-insensitive) first, else a UNIQUE substring.

        A term that matches one item verbatim drops it; otherwise a term that occurs in exactly one
        item drops that item — so the agent can remove a long sentence-item by a short distinctive
        fragment. A term that hits several items (ambiguous) or none is left alone and reported, so the
        agent retries with something more specific. `updated` is None if there is no such live list.
        """
        existing = await self.get(name)
        if existing is None:
            return RemoveResult(updated=None)

        result = RemoveResult(updated=existing)
        drop_targets: set[str] = set()
        for term in terms:
            hits = match_unique(existing.items, term)
            if len(hits) == 1:
                drop_targets.add(hits[0])
                result.removed.append(hits[0])
            elif len(hits) > 1:
                result.ambiguous.append(term)
            else:
                result.missing.append(term)

        if drop_targets:
            kept = [i for i in existing.items if i not in drop_targets]
            await self._collection.update_one(
                {"conversation_id": self._conversation_id, "name": _norm_name(name), "deleted_at": None},
                {"$set": {"items": kept, "updated_at": datetime.now()}},
            )
            existing.items = kept
        return result

    async def clear(self, name: str) -> bool:
        """Soft-delete the whole named list; True if a live one was found."""
        now = datetime.now()
        result = await self._collection.update_one(
            {"conversation_id": self._conversation_id, "name": _norm_name(name), "deleted_at": None},
            {"$set": {"deleted_at": now, "updated_at": now}},
        )
        return result.modified_count > 0
