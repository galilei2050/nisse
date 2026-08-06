"""Living prompts — per-conversation prompt fragments the bot maintains, keyed by type.

Unlike `memories` (discrete facts, recalled on demand by the agent), a prompt here is a single
document injected into the system prompt every turn. `core_memory` is the first type: the small,
always-on block of standing behaviour rules + canonical owner identity + current focus. One document
per `(conversation_id, prompt_type)`, overwritten in place.

The document has no second copy, so every overwrite first appends the block it replaces to
`revisions` — the only place a rewritten set of standing rules survives, and the only way the owner
can see what a nightly curator pass changed about how the bot behaves.
"""

from enum import StrEnum

from baski.primitives import datetime
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.models import NisseDbModel
from app.shared.mongo import ensure_index
from app.shared.revisions import ChangeKind, RevisionLog

_COLLECTION = "prompts"


class PromptType(StrEnum):
    """Which prompt a row holds. Extensible — a new kind is a new member, no schema change."""

    CORE_MEMORY = "core_memory"  # always-on block: behaviour rules + owner identity + current focus
    JUDGE_RULES = "judge_rules"  # lines appended to the completeness rubric the judge grades replies by


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
        self._revisions = RevisionLog(database, conversation_id=conversation_id)

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """Unique index on (conversation_id, prompt_type) — one row per type per chat. Idempotent."""
        await ensure_index(database[_COLLECTION], [("conversation_id", 1), ("prompt_type", 1)], unique=True)

    async def get(self, prompt_type: PromptType) -> str | None:
        """The current content for a prompt type in this conversation, or None if not set yet."""
        doc = await self._collection.find_one({"conversation_id": self._conversation_id, "prompt_type": prompt_type})
        return doc["content"] if doc else None

    async def set(self, prompt_type: PromptType, content: str) -> None:
        """Overwrite the prompt of this type in place (upsert), recording the text it replaces.

        This document has no second copy — the revision is the only place the previous block
        survives, and the only way the owner can see what a curator pass rewrote.
        """
        previous = await self.get(prompt_type)
        if previous == content:
            return  # an edit that matched nothing; recording it would inflate the owner's change count
        await self._revisions.record(
            collection=_COLLECTION,
            target=prompt_type,
            kind=ChangeKind.REPLACE if previous is not None else ChangeKind.CREATE,
            before=previous,
            after=content,
        )
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
