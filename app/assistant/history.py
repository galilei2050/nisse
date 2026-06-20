"""Persistent conversation transcript — a MessageHistory backed by MongoDB.

Schema: one document per turn in the `conversation_turns` collection:
    {conversation_id, turn_id, messages, next_turn_id, pruned_at}

`pruned_at` is the soft-delete marker (None = active). `load()` reads only
active turns (`pruned_at: None`). `prune_tool_turns()` marks old tool turns
pruned in Mongo and strips their tool blocks from the in-memory copy so the
final assistant text answer survives in the active transcript.
"""

from anthropic.types import MessageParam
from baski.agents import MessageHistory
from baski.agents.message_history import Turn
from baski.primitives import datetime
from baski.server import Logger
from pydantic import BaseModel
from pymongo.asynchronous.database import AsyncDatabase

_COLLECTION = "conversation_turns"


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


def _strip_tool_blocks(turn: Turn) -> Turn:
    """Return a copy of the turn with tool_use/tool_result blocks removed, keeping text blocks.

    A tool turn ends with an assistant message that contains the final text answer alongside
    (or after) tool_use blocks. Stripping tool blocks preserves that answer in the transcript
    so the agent still has context of what it said, without the bulky tool payloads.
    Messages that become empty after stripping (e.g. a pure tool_results user message) are
    dropped entirely.
    """
    kept: list[MessageParam] = []
    for msg in turn.messages:
        content = msg["content"]
        if isinstance(content, str):
            kept.append(msg)
            continue
        clean = [b for b in content if not _is_tool_block(b)]
        if clean:
            kept.append(MessageParam(role=msg["role"], content=clean))
    return Turn(id=turn.id, messages=kept)


class MongoMessageHistory(MessageHistory):
    """MessageHistory whose turns persist to MongoDB, one document per turn.

    Lifecycle: per-conversation — built with its `Conversation` and reused across replies.
    Call `load()` before the first reply to restore the active transcript, then `save()`
    after each reply to persist new turns. `prune_tool_turns()` soft-deletes old tool turns
    in Mongo (sets `pruned_at`) and strips their tool blocks from memory — the full history
    is always recoverable; only the active context window changes.
    """

    def __init__(self, *, logger: Logger, database: AsyncDatabase, conversation_id: int) -> None:
        """Bind the history to one conversation."""
        super().__init__(logger=logger)
        self._collection = database[_COLLECTION]
        self._conversation_id = conversation_id

    async def load(self) -> None:
        """Restore active (not pruned) turns for this conversation; no-op for a new one."""
        cursor = self._collection.find(
            {"conversation_id": self._conversation_id, "pruned_at": None},
            sort=[("turn_id", 1)],
        )
        docs = await cursor.to_list(length=None)
        if not docs:
            return
        self.turns = [Turn(id=doc["turn_id"], messages=list(doc["messages"])) for doc in docs]
        # next_turn_id is stored on every turn doc; the highest wins
        self._next_turn_id = max(doc["next_turn_id"] for doc in docs)

    def prune_tool_turns(self) -> list[int]:
        """Strip old tool-call turns from the active transcript, keeping only the most recent.

        Tool results (search/browse blobs) are bulky and re-derivable. We keep the most recent
        tool turn so follow-up questions can reference it. Older tool turns are soft-deleted in
        Mongo on the next `save()` — their final assistant text answers are preserved in the
        stripped in-memory copy so context isn't completely lost.

        Returns the list of turn IDs marked for pruning (empty if nothing to prune).
        """
        tool_turn_ids = [t.id for t in self.turns if _turn_has_tools(t)]
        if len(tool_turn_ids) <= 1:
            return []
        to_prune = set(tool_turn_ids[:-1])
        new_turns: list[Turn] = []
        for t in self.turns:
            if t.id in to_prune:
                stripped = _strip_tool_blocks(t)
                if stripped.messages:
                    new_turns.append(stripped)
                # turns with zero messages after stripping are fully dropped from memory
            else:
                new_turns.append(t)
        self.turns = new_turns
        self.logger.info(
            "Pruned tool turns before persist",
            labels={"pruned": sorted(to_prune), "keptLastToolTurn": tool_turn_ids[-1]},
        )
        return sorted(to_prune)

    async def save(self, *, pruned_turn_ids: list[int] | None = None) -> None:
        """Persist new turns and soft-delete pruned ones.

        Each turn is stored as a separate document keyed by (conversation_id, turn_id).
        Pruned turns already in Mongo have `pruned_at` set; they are never removed.
        """
        now = datetime.now()

        # Soft-delete pruned turns
        if pruned_turn_ids:
            await self._collection.update_many(
                {
                    "conversation_id": self._conversation_id,
                    "turn_id": {"$in": pruned_turn_ids},
                    "pruned_at": None,
                },
                {"$set": {"pruned_at": now}},
            )

        # Upsert each active turn (new or updated stripped version)
        for turn in self.turns:
            doc = {
                "conversation_id": self._conversation_id,
                "turn_id": turn.id,
                "messages": [_dump_message(m) for m in turn.messages],
                "next_turn_id": self._next_turn_id,
                "pruned_at": None,
            }
            await self._collection.update_one(
                {"conversation_id": self._conversation_id, "turn_id": turn.id},
                {"$set": doc},
                upsert=True,
            )
