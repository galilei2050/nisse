"""Long-term memory — durable owner-facts in Mongo.

Each doc keeps Mongo's `ObjectId` as `_id` (via NisseDbModel) plus a short `public_id`
the agent reads from the index and echoes back into read_memory/forget.
"""

import secrets
from enum import StrEnum

from baski.primitives import datetime
from pydantic import BaseModel, Field, model_validator
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.models import NisseDbModel

_COLLECTION = "memories"


class MemoryCategory(StrEnum):
    """What kind of thing a memory is — drives later curator decay/promotion."""

    FACT = "fact"  # stable truth about the owner/world
    PREFERENCE = "preference"  # how they like things done
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
    """Provenance of a memory: the owner, an external source, or the agent itself."""

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

    `public_id` is the short agent-facing key (≠ the DB `id`); audit timestamps and the
    soft-delete marker come from NisseDbModel.
    """

    public_id: str = Field(default_factory=_new_public_id)
    title: str
    category: MemoryCategory
    source: MemorySource
    body: str


class MemoryStore:
    """CRUD over the `memories` collection, addressed by the short public id."""

    def __init__(self, database: AsyncDatabase) -> None:
        """Bind to the shared database's memories collection."""
        self._collection = database[_COLLECTION]

    async def ensure_indexes(self) -> None:
        """Unique index on public_id — the agent-facing key. Idempotent; call once at startup."""
        await self._collection.create_index("public_id", unique=True)

    async def list(self) -> list[Memory]:
        """Every live memory (not soft-deleted), for the always-injected index."""
        return [Memory.model_validate(doc) async for doc in self._collection.find({"deleted_at": None})]

    async def get(self, public_id: str) -> Memory | None:
        """One live memory by its public id, or None if missing or soft-deleted."""
        doc = await self._collection.find_one({"public_id": public_id, "deleted_at": None})
        return Memory.model_validate(doc) if doc else None

    async def add(self, *, title: str, category: MemoryCategory, source: MemorySource, body: str) -> Memory:
        """Store a new memory; Mongo assigns `_id`, we keep the short public_id."""
        memory = Memory(title=title, category=category, source=source, body=body)
        result = await self._collection.insert_one(memory.model_dump(exclude={"id"}))
        memory.id = str(result.inserted_id)
        return memory

    async def soft_delete(self, public_id: str) -> bool:
        """Mark a memory deleted (keep the doc); True if a live one was found."""
        now = datetime.now()
        result = await self._collection.update_one(
            {"public_id": public_id, "deleted_at": None},
            {"$set": {"deleted_at": now, "updated_at": now}},
        )
        return result.modified_count > 0
