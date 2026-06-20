"""Persistent conversation transcript — a MessageHistory backed by MongoDB.

Schema: one Mongo document per turn in `conversation_turns`, modelled by `ConversationTurn`.
Standard audit fields (id/created_at/updated_at/deleted_at) come from NisseDbModel.
Turns are saved as-is and never modified; `deleted_at` is the only field that changes after insert.
"""

from anthropic.types import MessageParam
from baski.agents import MessageHistory
from baski.agents.message_history import Turn
from baski.primitives import datetime
from baski.server import Logger
from pydantic import BaseModel, ConfigDict
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.models import NisseDbModel

_COLLECTION = "conversation_turns"


def _dump_block(block: object) -> object:
    """JSON-safe a content block: Anthropic SDK blocks are Pydantic models, tool results are dicts."""
    return block.model_dump(mode="json", exclude_none=True) if isinstance(block, BaseModel) else block


def _dump_message(message: MessageParam) -> MessageParam:  # noqa: ANON002 — MessageParam is an Anthropic SDK TypedDict
    """Return a JSON-safe copy of a MessageParam with SDK block objects replaced by plain dicts."""
    content = message["content"]
    if isinstance(content, str):
        return MessageParam(role=message["role"], content=content)
    return MessageParam(role=message["role"], content=[_dump_block(b) for b in content])  # type: ignore[misc]  # list[object] → content union at runtime (SDK blocks → plain dicts)


class ConversationTurn(NisseDbModel):
    """One agent turn persisted to Mongo — all messages for one reply cycle.

    Lifecycle: a data record — one document per turn per conversation. `turn_id` is baski's
    sequential Turn.id and, together with `conversation_id`, forms the upsert key. Audit timestamps
    and the soft-delete marker (`deleted_at`) come from NisseDbModel. Content is never modified;
    only `deleted_at` is stamped when a turn is pruned from the active transcript.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    conversation_id: int
    turn_id: int  # baski Turn.id — sequential int, upsert key with conversation_id
    messages: list[MessageParam]  # stored as serialised plain dicts; MessageParam is a TypedDict (= dict at runtime)


def _block_type(block: object) -> str | None:
    """Return the type field of a content block (dict or SDK object)."""
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _has_text_answer(turn: Turn) -> bool:
    """True if the turn has at least one assistant message with a text block (a user-facing answer)."""
    for msg in turn.messages:
        if msg["role"] != "assistant":
            continue
        content = msg["content"]
        if isinstance(content, str):
            return True
        if any(_block_type(b) == "text" for b in content):
            return True
    return False


class MongoMessageHistory(MessageHistory):
    """MessageHistory whose turns persist to MongoDB as `ConversationTurn` documents.

    Lifecycle: per-conversation — built with its `Conversation` and reused across replies.
    Call `load()` before the first reply to restore the active transcript, then `save()` after
    each reply. `save()` persists every turn to Mongo first (so the full history is always
    recoverable), then soft-deletes the turns with no user-facing answer and drops them from
    the active transcript. Turn content is never modified — whole turns are kept or soft-deleted.
    """

    def __init__(self, *, logger: Logger, database: AsyncDatabase, conversation_id: int) -> None:
        """Bind the history to one conversation."""
        super().__init__(logger=logger)
        self._collection = database[_COLLECTION]
        self._conversation_id = conversation_id

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """Compound indexes for per-conversation queries. Idempotent; call once at startup."""
        col = database[_COLLECTION]
        await col.create_index([("conversation_id", 1), ("turn_id", 1)], unique=True)
        await col.create_index([("conversation_id", 1), ("deleted_at", 1)])

    async def load(self) -> None:
        """Restore active turns (deleted_at=None) for this conversation; no-op for a new one."""
        cursor = self._collection.find(
            {"conversation_id": self._conversation_id, "deleted_at": None},
            sort=[("turn_id", 1)],
        )
        docs = await cursor.to_list(length=None)
        if not docs:
            return
        # Read messages directly from raw Mongo docs — bypassing TypedDict validation so plain
        # dicts from Mongo are not re-wrapped in Pydantic ValidatorIterator objects that would
        # break the Anthropic SDK serializer when the agent formats messages for the API.
        self.turns = [Turn(id=doc["turn_id"], messages=list(doc["messages"])) for doc in docs]
        self._next_turn_id = max(doc["turn_id"] for doc in docs)

    async def save(self) -> None:
        """Persist every turn to Mongo, then soft-delete the ones with no user-facing answer.

        Two phases, in order, so the full history is always recoverable:
        1. Upsert every active turn as a full document — content serialised as-is, never modified.
           A turn with no assistant text answer is written with `deleted_at` already set, so even
           a turn pruned the moment it was created lands in Mongo and can be restored.
        2. Drop those pruned turns from the active in-memory transcript so the next reply's context
           excludes them. Their documents remain in Mongo.
        """
        now = datetime.now()
        prunable = {turn.id for turn in self.turns if not _has_text_answer(turn)}

        for turn in self.turns:
            deleted_at = now if turn.id in prunable else None
            await self._collection.update_one(
                {"conversation_id": self._conversation_id, "turn_id": turn.id},
                {
                    "$set": {
                        "conversation_id": self._conversation_id,
                        "turn_id": turn.id,
                        "messages": [_dump_message(m) for m in turn.messages],
                        "updated_at": now,
                        "deleted_at": deleted_at,
                    },
                    "$setOnInsert": {"created_at": now},  # only on first insert, not on updates
                },
                upsert=True,
            )

        self.turns = [turn for turn in self.turns if turn.id not in prunable]
        if prunable:
            self.logger.info("Soft-deleted turns with no user-facing answer", labels={"deleted": sorted(prunable)})
