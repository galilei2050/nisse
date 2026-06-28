"""Living prompts — per-conversation prompt fragments the bot maintains, keyed by type.

Unlike `memories` (discrete facts, recalled on demand by the agent), a prompt here is a single
document injected into the system prompt every turn. `core_memory` is the first type: the small,
always-on block of standing behaviour rules + canonical owner identity + current focus. One document
per `(conversation_id, prompt_type)`, overwritten in place — no soft-delete, no versioning.
"""

from enum import StrEnum

from baski.primitives import datetime
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.models import NisseDbModel
from app.shared.mongo import ensure_index

_COLLECTION = "prompts"


class PromptType(StrEnum):
    """Which prompt a row holds. Extensible — a new kind is a new member, no schema change."""

    CORE_MEMORY = "core_memory"  # always-on block: behaviour rules + owner identity + current focus


class Prompt(NisseDbModel):
    """One living prompt document, scoped to a conversation and addressed by its type."""

    conversation_id: int
    prompt_type: PromptType
    content: str


class PromptStore:
    """Read/write the `prompts` collection for one conversation. Lifecycle: per-conversation."""

    def __init__(self, database: AsyncDatabase, *, conversation_id: int) -> None:
        """Bind to the prompts collection for one conversation; every query is scoped to it."""
        self._collection = database[_COLLECTION]
        self._conversation_id = conversation_id

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """Unique index on (conversation_id, prompt_type) — one row per type per chat. Idempotent."""
        await ensure_index(database[_COLLECTION], [("conversation_id", 1), ("prompt_type", 1)], unique=True)

    async def get(self, prompt_type: PromptType) -> str | None:
        """The current content for a prompt type in this conversation, or None if not set yet."""
        doc = await self._collection.find_one({"conversation_id": self._conversation_id, "prompt_type": prompt_type})
        return doc["content"] if doc else None

    async def set(self, prompt_type: PromptType, content: str) -> None:
        """Overwrite the prompt of this type in place (upsert) — the document is replaced wholesale."""
        now = datetime.now()
        await self._collection.update_one(
            {"conversation_id": self._conversation_id, "prompt_type": prompt_type},
            {
                "$set": {"content": content, "updated_at": now},
                "$setOnInsert": {
                    "conversation_id": self._conversation_id,
                    "prompt_type": prompt_type,
                    "created_at": now,
                },
            },
            upsert=True,
        )
