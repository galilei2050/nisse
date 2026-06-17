"""Persistent conversation transcript — a MessageHistory backed by MongoDB."""

from anthropic.types import MessageParam
from baski.agents import MessageHistory
from baski.agents.message_history import Turn
from baski.server import Logger
from pydantic import BaseModel
from pymongo.asynchronous.database import AsyncDatabase

_COLLECTION = "conversations"


def _dump_block(block: object) -> object:
    """JSON-safe a content block: anthropic SDK blocks are pydantic, tool results are dicts."""
    return block.model_dump(mode="json", exclude_none=True) if isinstance(block, BaseModel) else block


def _dump_message(message: MessageParam) -> object:
    """Serialize one message to a JSON-safe doc; content is a list of blocks (or a string)."""
    content = message["content"]
    if isinstance(content, str):
        return {"role": message["role"], "content": content}
    return {"role": message["role"], "content": [_dump_block(b) for b in content]}


class MongoMessageHistory(MessageHistory):
    """MessageHistory whose turns persist to MongoDB, one document per conversation.

    The agent loop mutates turns in memory (base class). Call `load()` before the loop to
    restore a prior conversation and `save()` after it to persist the new state — including
    any truncation/deletion the agent did, so context stays bounded across replies.
    """

    def __init__(self, *, logger: Logger, database: AsyncDatabase, conversation_id: int) -> None:
        """Bind the history to one conversation's document."""
        super().__init__(logger=logger)
        self._collection = database[_COLLECTION]
        self._conversation_id = conversation_id

    async def load(self) -> None:
        """Restore persisted turns for this conversation (no-op for a new one)."""
        doc = await self._collection.find_one({"_id": self._conversation_id})
        if doc is None:
            return
        self.turns = [Turn(id=turn["id"], messages=list(turn["messages"])) for turn in doc["turns"]]
        self._next_turn_id = doc["next_turn_id"]

    async def save(self) -> None:
        """Persist the current turns for this conversation."""
        doc = {
            "_id": self._conversation_id,
            "turns": [{"id": turn.id, "messages": [_dump_message(m) for m in turn.messages]} for turn in self.turns],
            "next_turn_id": self._next_turn_id,
        }
        await self._collection.replace_one({"_id": self._conversation_id}, doc, upsert=True)
