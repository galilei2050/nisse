"""LONG-TERM MEMORY — durable owner-facts in Mongo.

Each doc keeps Mongo's `ObjectId` as `_id` (via NisseDbModel) plus a short `public_id`
the agent reads from the index and echoes back into recall_read/recall_forget.
"""

import secrets
from enum import StrEnum

from baski.primitives import datetime
from pydantic import BaseModel, Field, model_validator
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.models import NisseDbModel

_COLLECTION = "memories"


class MemoryCategory(StrEnum):
    """What kind of thing a memory is — drives later curator decay/promotion.

    Behavioural preferences and identity that shape how the bot acts live in core memory
    (`app/prompts`, always-on), NOT here — `memories` is the recalled-on-demand long tail.
    """

    FACT = "fact"  # stable truth about the owner/world, recalled when its topic comes up
    EVENT = "event"  # something dated, past or future


class SourceKind(StrEnum):
    """Where a memory came from."""

    USER = "user"
    EXTERNAL = "external"  # ref carries the url/name
    AGENT = "agent"


def _new_public_id() -> str:
    """A short, LLM-friendly id (10 hex chars) the agent can copy back without error."""
    return secrets.token_hex(5)


class MemorySource(BaseModel):
    """Provenance of a memory: the owner, an external source, or the agent itself. Lifecycle: a value object."""

    kind: SourceKind
    ref: str | None = None  # url / name when kind == "external"

    @model_validator(mode="after")
    def _external_needs_ref(self) -> "MemorySource":
        """An external source must name where it came from — a url or name, not blank."""
        if self.kind is SourceKind.EXTERNAL and not (self.ref and self.ref.strip()):
            raise ValueError("source.ref is required (the url or name) when source.kind is 'external'")
        return self


class Memory(NisseDbModel):
    """One durable memory: a titled fact/preference/event with provenance and body.

    Lifecycle: a data record — one Mongo document. Scoped to one `conversation_id` (the chat it was
    learned in) — memories never cross conversations. `public_id` is the short agent-facing key (≠ the
    DB `id`); audit timestamps and the soft-delete marker come from NisseDbModel.
    """

    conversation_id: int
    public_id: str = Field(default_factory=_new_public_id)
    title: str
    category: MemoryCategory
    source: MemorySource
    body: str


class MemoryStore:
    """CRUD over the `memories` collection, scoped to one conversation and addressed by public id.

    Lifecycle: per-conversation — built in `_build_memory_tools` and held by that chat's tools.
    """

    def __init__(self, database: AsyncDatabase, *, conversation_id: int) -> None:
        """Bind to the memories collection for one conversation; every query is scoped to it."""
        self._collection = database[_COLLECTION]
        self._conversation_id = conversation_id

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """Unique index on public_id — the agent-facing key. Idempotent; call once at startup."""
        await database[_COLLECTION].create_index("public_id", unique=True)

    async def list(self) -> list[Memory]:
        """Every live memory in this conversation (not soft-deleted), for the always-injected index."""
        query = {"conversation_id": self._conversation_id, "deleted_at": None}
        return [Memory.model_validate(doc) async for doc in self._collection.find(query)]

    async def get(self, public_id: str) -> Memory | None:
        """One live memory by its public id within this conversation, or None if missing/deleted."""
        doc = await self._collection.find_one(
            {"conversation_id": self._conversation_id, "public_id": public_id, "deleted_at": None}
        )
        return Memory.model_validate(doc) if doc else None

    async def add(self, *, title: str, category: MemoryCategory, source: MemorySource, body: str) -> Memory:
        """Store a new memory in this conversation; Mongo assigns `_id`, we keep the short public_id."""
        memory = Memory(conversation_id=self._conversation_id, title=title, category=category, source=source, body=body)
        result = await self._collection.insert_one(memory.model_dump(exclude={"id"}))
        memory.id = str(result.inserted_id)
        return memory

    async def overwrite(  # noqa: PLR0913 — mirrors add() plus the public_id of the record to overwrite
        self, public_id: str, *, title: str, category: MemoryCategory, source: MemorySource, body: str
    ) -> Memory:
        """Replace a live memory's fields in place by public id; create a fresh one if the id is gone.

        A fresh create (rather than reusing the requested id) is deliberate: a missing id means the
        memory was soft-deleted, and its `public_id` still occupies the unique index — reusing it would
        collide. The caller reports whichever id is now live.
        """
        result = await self._collection.find_one_and_update(
            {"conversation_id": self._conversation_id, "public_id": public_id, "deleted_at": None},
            {
                "$set": {
                    "title": title,
                    "category": category,
                    "source": source.model_dump(),
                    "body": body,
                    "updated_at": datetime.now(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            return await self.add(title=title, category=category, source=source, body=body)
        return Memory.model_validate(result)

    async def set_body(self, public_id: str, *, body: str) -> None:
        """Overwrite one live memory's body (the agent's recall_edit patch); bump updated_at."""
        await self._collection.update_one(
            {"conversation_id": self._conversation_id, "public_id": public_id, "deleted_at": None},
            {"$set": {"body": body, "updated_at": datetime.now()}},
        )

    async def soft_delete(self, public_id: str) -> bool:
        """Mark a memory deleted (keep the doc); True if a live one was found in this conversation."""
        now = datetime.now()
        result = await self._collection.update_one(
            {"conversation_id": self._conversation_id, "public_id": public_id, "deleted_at": None},
            {"$set": {"deleted_at": now, "updated_at": now}},
        )
        return result.modified_count > 0
