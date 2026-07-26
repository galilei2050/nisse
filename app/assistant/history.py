"""Persistent conversation transcript — a `MessageHistory` implementation backed by MongoDB.

A standalone implementation of baski's `MessageHistory` Protocol (not a subclass of the in-memory
one): it owns both the in-memory active transcript and its durable Mongo backing.

Durability model — write-as-you-go, await-after-send:
- Each turn is written to Mongo the moment it completes (`__exit__` fires a fire-and-forget task).
  So a crash mid-reply still leaves every finished turn on its way to disk.
- The write tasks are collected; `flush()` awaits them. The chat router calls `flush()` AFTER the
  answer is sent to the user, so Mongo latency never delays the reply.
- A turn is written exactly once and never rewritten; `deleted_at` is the only field that changes
  after insert (set when a turn is pruned, truncated, or deleted). Soft-deleted turns keep full
  content — recoverable.

Schema: one Mongo document per turn in `conversation_turns`, modelled by `ConversationTurn`.
Single-writer: turn ids are minted in memory, correct only at `max_instances=1` (see
infrastructure/services/cloud_run_backend.py) with every entry point sharing one cached agent.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Self, cast

from anthropic.types import (
    ContentBlock,
    DocumentBlockParam,
    ImageBlockParam,
    MessageParam,
    TextBlockParam,
    ToolResultBlockParam,
    Usage,
)
from anthropic.types.base64_image_source_param import Base64ImageSourceParam
from anthropic.types.base64_pdf_source_param import Base64PDFSourceParam
from baski.agents.message_history import MessageHistory, Turn, context_status, mark_cached
from baski.agents.pricing import effective_input_tokens
from baski.primitives import datetime
from pydantic import BaseModel, ConfigDict
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.blocks import block_type
from app.shared.models import NisseDbModel
from app.shared.mongo import ensure_index

logger = logging.getLogger(__name__)

_ImageMediaType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]

_COLLECTION = "conversation_turns"
_MAX_TOKENS = 32_000  # context budget: truncate() trims oldest turns as effective input nears this
_TRUNCATE_THRESHOLD = 0.9
_TRUNCATE_PERCENTAGE = 0.3
_GAP_MARKER_THRESHOLD = datetime.timedelta(hours=1)  # show the send-time on a turn only after a gap this long


@dataclass
class MongoTurn(Turn):
    """A transcript turn that also carries its UTC send-time, used to render the recency marker."""

    created_at: datetime.datetime = field(kw_only=True)


def _turn_marker(turn_id: int, at: datetime.datetime, prev_at: datetime.datetime | None) -> str:
    """`[Turn N]`, plus the turn's absolute UTC send-time on the first turn or after a >1h gap.

    Absolute (not relative) to stay byte-stable in the cached prefix.
    """
    if prev_at is not None and at - prev_at <= _GAP_MARKER_THRESHOLD:
        return f"[Turn {turn_id}]"
    return f"[Turn {turn_id} · {at.strftime('%Y-%m-%d %H:%M')} UTC]"


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


def _is_text_block(block: object) -> bool:
    """True if a content block is a text block (by its Anthropic `type` discriminator)."""
    return block_type(block) == "text"


def _is_thinking_block(block: object) -> bool:
    """True for a thinking or redacted-thinking block (by its Anthropic `type` discriminator)."""
    return block_type(block) in ("thinking", "redacted_thinking")


def _strip_thinking(message: MessageParam) -> MessageParam:  # noqa: ANON002 — MessageParam is an Anthropic SDK TypedDict
    """Drop thinking/redacted_thinking blocks from a completed turn's message.

    On Opus 4.5+/Sonnet 4.6+ prior-turn thinking is kept in context and billed as input tokens, yet
    it's only needed for tool-use continuation *within* the active turn — which rides on the still-open
    in-flight turn, never on `self._turns`. So for settled turns the encrypted reasoning is dead weight;
    the API permits omitting (not modifying) prior thinking blocks. Mongo keeps the full block.
    """
    content = message["content"]
    if isinstance(content, str):
        return message
    blocks = list(content)
    kept = [b for b in blocks if not _is_thinking_block(b)]
    if len(kept) == len(blocks):
        return message
    return MessageParam(role=message["role"], content=kept)


def _has_text(turn: Turn) -> bool:
    """True if the turn has any conversational text — a user question or an assistant reply.

    A turn with text is part of the visible conversation and is kept. A turn with none is pure
    tool machinery (only tool_use/tool_result blocks) and is the only kind that gets pruned.
    """
    for msg in turn.messages:
        content = msg["content"]
        if isinstance(content, str):
            return True
        if any(_is_text_block(b) for b in content):
            return True
    return False


class MongoMessageHistory(MessageHistory):
    """A `MessageHistory` whose turns persist to MongoDB as `ConversationTurn` documents.

    Lifecycle: per-conversation — built with its `Conversation` and reused across replies. Call
    `load()` before the first reply to restore the active transcript. During a reply each completed
    turn is written fire-and-forget (`__exit__`); `flush()` awaits those writes after the answer is
    sent. `drop_tool_turns()` removes pure tool turns from the active transcript between replies.
    """

    def __init__(self, *, database: AsyncDatabase, conversation_id: int) -> None:
        """Bind the history to one conversation and start with an empty in-memory transcript."""
        self._collection = database[_COLLECTION]
        self._conversation_id = conversation_id

        # In-memory transcript + turn assembly (Protocol surface).
        self._turns: list[MongoTurn] = []
        self.max_tokens = _MAX_TOKENS
        self._next_turn_id = 0
        self._current_turn: Turn | None = None
        self._last_input_tokens = 0

        # Durable write bookkeeping.
        self._writes: list[asyncio.Task[None]] = []  # in-flight fire-and-forget turn inserts
        self._dropped: set[int] = set()  # turn ids removed from context (truncate/delete) to soft-delete on flush

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """Compound indexes for per-conversation queries. Idempotent; call once at startup."""
        col = database[_COLLECTION]
        await ensure_index(col, [("conversation_id", 1), ("turn_id", 1)], unique=True)
        await ensure_index(col, [("conversation_id", 1), ("deleted_at", 1), ("turn_id", 1)])

    # --- MessageHistory protocol: in-memory turn assembly ---

    @property
    def turns(self) -> Sequence[MongoTurn]:
        """Committed turns, oldest first — read-only; mutation goes through the contract methods."""
        return self._turns

    def __len__(self) -> int:
        """Number of committed turns in the active transcript."""
        return len(self._turns)

    def __enter__(self) -> Self:
        """Open a new turn, assigning the next sequential id and stamping its send-time."""
        self._next_turn_id += 1
        self._current_turn = MongoTurn(id=self._next_turn_id, created_at=datetime.now())
        return self

    def __exit__(self, *args: object) -> None:
        """Commit the open turn and fire its durable write (fire-and-forget; awaited in flush())."""
        turn = self._current_turn
        self._current_turn = None
        if turn and turn.messages:
            self._turns.append(turn)
            self._writes.append(asyncio.create_task(self._write_turn(turn)))

    @property
    def _turn(self) -> Turn:
        """The open turn, raising if used outside the context manager."""
        if self._current_turn is None:
            raise RuntimeError("No active turn; use the history as a context manager first")
        return self._current_turn

    def add_assistant(self, content_blocks: list[ContentBlock]) -> None:
        """Append the assistant's message (text/tool_use/thinking blocks) to the open turn."""
        self._turn.messages.append(MessageParam(role="assistant", content=content_blocks))

    def add_tool_results(self, results: list[ToolResultBlockParam]) -> None:
        """Append the tool_result blocks for this round to the open turn."""
        self._turn.messages.append(MessageParam(role="user", content=results))

    def add_user_text(self, text: str) -> None:
        """Append a plain user-text message to the open turn."""
        self._turn.messages.append(MessageParam(role="user", content=[TextBlockParam(type="text", text=text)]))

    def add_photo(self, *, data: str, media_type: str) -> None:
        """Append a user image message — a photo the model reads as vision (media_type checked upstream)."""
        source = Base64ImageSourceParam(type="base64", media_type=cast("_ImageMediaType", media_type), data=data)
        self._turn.messages.append(MessageParam(role="user", content=[ImageBlockParam(type="image", source=source)]))

    def add_document(self, *, data: str) -> None:
        """Append a user PDF-document message the model reads natively."""
        source = Base64PDFSourceParam(type="base64", media_type="application/pdf", data=data)
        self._turn.messages.append(
            MessageParam(role="user", content=[DocumentBlockParam(type="document", source=source)])
        )

    def format_for_api(self) -> list[MessageParam]:
        """Render the transcript with [Turn N] markers; cache breakpoint on the last turn (thinking stripped)."""
        result: list[MessageParam] = []
        prev_at: datetime.datetime | None = None
        for turn in self._turns:
            at = datetime.as_utc(turn.created_at)
            marker = _turn_marker(turn.id, at, prev_at)
            result.append(MessageParam(role="user", content=[TextBlockParam(type="text", text=marker)]))
            result.extend(_strip_thinking(m) for m in turn.messages)
            prev_at = at

        if result:
            result[-1] = mark_cached(result[-1])
        return result

    def context_status(self) -> MessageParam | None:
        """The context-usage footer, rendered by the shared helper from this history's counters."""
        return context_status(self._last_input_tokens, self.max_tokens)

    def initial_context_too_large(self, input_tokens: int) -> bool:
        """True when the transcript is empty yet the first request already exceeds half the budget."""
        return not self._turns and input_tokens > self.max_tokens // 2

    def truncate(self, usage: Usage) -> None:
        """Drop oldest turns when input-token usage exceeds the budget; mark them for soft-delete."""
        context_tokens = effective_input_tokens(usage)
        self._last_input_tokens = context_tokens
        if context_tokens < int(self.max_tokens * _TRUNCATE_THRESHOLD) or not self._turns:
            return
        count = max(int(len(self._turns) * _TRUNCATE_PERCENTAGE), 1)
        dropped, self._turns = self._turns[:count], self._turns[count:]
        self._dropped.update(turn.id for turn in dropped)
        logger.info(
            "Truncated message history",
            extra={"inputTokens": context_tokens, "turnsRemoved": count, "turnsAfter": len(self._turns)},
        )

    async def delete_turns(self, turn_ids: list[int]) -> int:
        """Remove whole turns by id from context; their soft-delete is persisted on the next flush()."""
        ids = set(turn_ids)
        original = len(self._turns)
        self._turns = [turn for turn in self._turns if turn.id not in ids]
        removed = original - len(self._turns)
        self._dropped.update(ids)
        logger.info("Turns deleted by agent", extra={"turnIds": sorted(ids), "turnsRemoved": removed})
        return removed

    # --- persistence ---

    async def load(self) -> None:
        """Restore active turns and advance the turn-id counter past every turn (incl. soft-deleted).

        The counter is set from the highest turn_id EVER used so `__enter__` never re-issues a
        soft-deleted turn's id and collides on the unique index.
        """
        active = await self._collection.find(
            {"conversation_id": self._conversation_id, "deleted_at": None},
            sort=[("turn_id", 1)],
        ).to_list(length=None)
        # Read messages straight from raw Mongo docs — bypassing TypedDict validation so plain dicts
        # are not re-wrapped in Pydantic ValidatorIterator objects that break the SDK serializer.
        self._turns = [
            MongoTurn(id=doc["turn_id"], messages=list(doc["messages"]), created_at=doc["created_at"]) for doc in active
        ]

        newest = await self._collection.find_one(
            {"conversation_id": self._conversation_id},
            sort=[("turn_id", -1)],
        )
        self._next_turn_id = newest["turn_id"] if newest else 0

    async def flush(self) -> None:
        """Await the in-flight turn writes, then persist soft-deletes. Called after the reply is sent.

        Inserts are gathered FIRST so a soft-delete never races ahead of the insert it targets.
        """
        writes, self._writes = self._writes, []
        if writes:
            # return_exceptions=True so every sibling write settles (no orphaned tasks) before we
            # surface a failure; raising here leaves _dropped untouched below → retried next flush.
            for result in await asyncio.gather(*writes, return_exceptions=True):
                if isinstance(result, BaseException):
                    raise result

        dropped, self._dropped = self._dropped, set()
        if dropped:
            now = datetime.now()
            await self._collection.update_many(
                {"conversation_id": self._conversation_id, "turn_id": {"$in": list(dropped)}, "deleted_at": None},
                {"$set": {"deleted_at": now, "updated_at": now}},
            )

    def drop_tool_turns(self) -> None:
        """Drop pure tool turns from the active transcript so the next reply's context stays lean.

        Their Mongo docs were already written soft-deleted (see `_write_turn`), so this is an
        in-memory prune only — no extra write, and the full turn stays recoverable in Mongo.
        """
        self._turns = [turn for turn in self._turns if _has_text(turn)]

    async def _write_turn(self, turn: Turn) -> None:
        """Insert one turn document, once. A pure tool turn is written already soft-deleted."""
        now = datetime.now()
        deleted_at = None if _has_text(turn) else now
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
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
