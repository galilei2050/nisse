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


def _is_tool_block(block: object) -> bool:
    """True for a tool_use/tool_result block — SDK object (assistant) or raw dict (tool result)."""
    kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
    return kind in ("tool_use", "tool_result")


def _turn_has_tools(turn: Turn) -> bool:
    """True if any message in the turn carries a tool_use/tool_result block."""
    return any(
        _is_tool_block(b) for m in turn.messages for b in (m["content"] if isinstance(m["content"], list) else [])
    )


class MongoMessageHistory(MessageHistory):
    """MessageHistory whose turns persist to MongoDB, one document per conversation.

    Lifecycle: per-conversation — built with its `Conversation` and reused across replies. The agent
    loop mutates turns in memory (base class). Call `load()` before the loop to restore a prior
    conversation and `save()` after it to persist the new state — including any truncation/deletion
    the agent did, so context stays bounded across replies.
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

    def prune_tool_turns(self) -> None:
        """Drop tool-call turns from the transcript, keeping only the most recent one.

        Tool results (search/browse blobs) are the bulk of the context and re-derivable; durable
        owner facts already live in long-term memory. Called after each reply, before persisting:
        keep the last tool turn so an immediate follow-up can still reference it, drop the older
        ones — bounding context without the agent having to manage it.
        """
        tool_turn_ids = [t.id for t in self.turns if _turn_has_tools(t)]
        if len(tool_turn_ids) <= 1:
            return
        drop = set(tool_turn_ids[:-1])
        self.turns = [t for t in self.turns if t.id not in drop]
        self.logger.info(
            "Pruned tool turns before persist",
            labels={"dropped": sorted(drop), "keptLastToolTurn": tool_turn_ids[-1]},
        )

    async def save(self) -> None:
        """Persist the current turns for this conversation."""
        doc = {
            "_id": self._conversation_id,
            "turns": [{"id": turn.id, "messages": [_dump_message(m) for m in turn.messages]} for turn in self.turns],
            "next_turn_id": self._next_turn_id,
        }
        await self._collection.replace_one({"_id": self._conversation_id}, doc, upsert=True)
