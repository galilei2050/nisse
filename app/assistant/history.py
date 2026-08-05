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
from enum import StrEnum
from typing import Literal, Self, TypedDict, cast

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
from pydantic import BaseModel, ConfigDict, Field
from pymongo.asynchronous.database import AsyncDatabase

from app.shared.blocks import block_type
from app.shared.models import NisseDbModel
from app.shared.mongo import ensure_index

logger = logging.getLogger(__name__)

_ImageMediaType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]

TURNS_COLLECTION = "conversation_turns"  # public: the curator reads this collection too
# The judge's retry feedback re-enters the loop as a USER message (baski `judge.retry_prompt`) and is
# persisted like any other turn, so anything reading the transcript back as "what the owner said"
# must skip it. Mirrored here rather than imported because baski builds the string, not the prefix.
JUDGE_RETRY_PREFIX = "[Completeness check]"
_MAX_TOKENS = 32_000  # context budget: compact() starts shrinking as effective input nears this
_TRUNCATE_THRESHOLD = 0.9
_TRUNCATE_PERCENTAGE = 0.3
_GAP_MARKER_THRESHOLD = datetime.timedelta(hours=1)  # show the send-time on a turn only after a gap this long
_PAYLOAD_RETENTION = datetime.timedelta(hours=1)  # how long a turn keeps its tool payloads/attachments


class ForgetReason(StrEnum):
    """Why a turn left the active transcript — a log dimension, so the values are filtered on."""

    NO_TEXT = "no-text"  # a pure tool round: nothing was said in it
    OVER_BUDGET = "over-budget"  # the context outgrew the budget and the oldest exchanges went
    AGENT = "agent"  # the agent's own prune_transcript call


@dataclass
class MongoTurn(Turn):
    """A transcript turn that also carries its UTC send-time, used to render the recency marker."""

    # baski ships no `py.typed`, so `Turn` reaches mypy as `Any` and the inherited field has no type
    # it can resolve — every `self.messages` here then fails with "Cannot determine type". Restating
    # the same declaration gives this class one concrete type back.
    messages: list[MessageParam] = field(default_factory=list)
    created_at: datetime.datetime = field(kw_only=True)

    def keep_only_text(self) -> bool:
        """Strip every block except `text`; return True if anything was removed.

        Kept: `text` blocks — the owner's message, the agent's answer. Dropped: `tool_use` calls,
        their `tool_result` payloads, attached images/PDFs and `thinking`. A message left with no
        blocks is dropped whole.

        A call and its result are always in the same turn, so both leave together and no `tool_use` is
        left without its `tool_result`.
        """
        kept: list[MessageParam] = []
        for message in self.messages:
            content = message["content"]
            if isinstance(content, str):
                kept.append(message)
                continue
            blocks = list(content)
            texts = [b for b in blocks if _is_text_block(b)]
            if not texts:
                continue
            kept.append(message if len(texts) == len(blocks) else MessageParam(role=message["role"], content=texts))
        # Compared against the thinking-stripped form, because that is what `format_for_api` already
        # sends for a settled turn. Dropping only thinking frees nothing, and reporting it as a
        # reduction would keep starving the cut that does free something.
        if kept == [_strip_thinking(message) for message in self.messages]:
            return False
        self.messages = kept
        return True


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
    message_ids: list[int] = Field(default_factory=list)  # Telegram messages this turn's answer was delivered in


class StoredBlock(TypedDict, total=False):
    """One content block as Mongo holds it. Lifecycle: a read view over one stored field.

    Neither key is required — a tool_use or image block legitimately carries neither.
    """

    type: str
    text: str


class StoredMessage(TypedDict):
    """One message as Mongo holds it. Lifecycle: a read view over one stored field.

    Deliberately not the SDK's `MessageParam` that `ConversationTurn` writes: what comes back is
    JSON, and reading it as the type it actually has beats narrowing a union of twenty block shapes
    to find the text.
    """

    role: str
    content: str | list[StoredBlock]


class StoredTurn(TypedDict):
    """One `conversation_turns` document as a reader outside this module sees it.

    The read half of `ConversationTurn` — same document, declared once here rather than a second time
    wherever it is read back (the nightly curator is the other reader).

    Lifecycle: a read view — one raw Mongo document.
    """

    turn_id: int
    created_at: datetime.datetime
    messages: list[StoredMessage]


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
    sent. Between replies `compact()` is the only thing that shrinks the transcript, and `_forget()`
    the only way a turn leaves it.
    """

    def __init__(self, *, database: AsyncDatabase, conversation_id: int) -> None:
        """Bind the history to one conversation and start with an empty in-memory transcript."""
        self._collection = database[TURNS_COLLECTION]
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
        col = database[TURNS_COLLECTION]
        await ensure_index(col, [("conversation_id", 1), ("turn_id", 1)], unique=True)
        await ensure_index(col, [("conversation_id", 1), ("deleted_at", 1), ("turn_id", 1)])
        await ensure_index(col, [("conversation_id", 1), ("message_ids", 1)])  # TurnLookup's reverse lookup

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
        """Record this call's context size. The transcript itself is only ever shrunk by `compact()`.

        baski calls this after every API call of the loop. Dropping turns here would move the head of
        the message list mid-run, so the cached prefix stops matching and the whole transcript is
        re-written at 1.25x on every remaining turn instead of read back at 0.1x.
        """
        self._last_input_tokens = effective_input_tokens(usage)

    def compact(self) -> None:
        """Shrink the transcript. The only method that does, and it runs once the agent loop has ended.

        Never call it from inside the loop: the loop has to see a transcript that only grows, or the
        cached prefix stops matching and the reply loses context it is still composing against.

        Three cuts:

        1. turns with no text at all go — nothing was said in them, so this one always runs;
        2. over budget: turns older than `_PAYLOAD_RETENTION` lose their tool calls, results,
           attachments and thinking, and keep their text;
        3. over budget with nothing left to strip: whole turns go, oldest first.

        Only 2 and 3 cost the owner anything, and both wait for the context to actually reach the
        budget — a small conversation keeps every photo it was sent, however old. The budget is read
        from `_last_input_tokens`: the measured size of the run that just finished, not a local guess
        at token counts. One cut per reply, and the next reply's measurement says whether it was
        enough.
        """
        shrunk = self._forget({turn.id for turn in self._turns if not _has_text(turn)}, reason=ForgetReason.NO_TEXT) > 0
        if self._last_input_tokens >= int(self.max_tokens * _TRUNCATE_THRESHOLD):
            if not self._reduce_old_turns_to_text():
                self._drop_oldest_turns()
            shrunk = True
        if shrunk:
            # The recorded size describes the transcript as it was BEFORE these cuts. Left standing, it
            # makes `context_status()` report a fullness that no longer exists — "[Context: 187% used]"
            # after a drop — which reads to the agent as an order to prune. Zero omits the footer until
            # the next API call measures the real thing.
            self._last_input_tokens = 0

    def _reduce_old_turns_to_text(self) -> bool:
        """Strip everything but text from turns past `_PAYLOAD_RETENTION`; True if anything was removed.

        An hour, because a follow-up reaches into the output of the exchange it follows — "show me the
        second one you found". 85% of the owner's messages arrive within an hour of the previous one
        (1,007 gaps, June-August 2026). Tool payloads and attachments were 45% of the live context when
        that was measured, so dropping the old ones buys most of the room a shorter transcript would.

        In memory only — Mongo keeps every turn whole, so what was cut stays readable and recoverable.
        """
        cutoff = datetime.now() - _PAYLOAD_RETENTION
        reduced = [
            turn.id for turn in self._turns if datetime.as_utc(turn.created_at) <= cutoff and turn.keep_only_text()
        ]
        if reduced:
            # Which turns, not just how many: "why did you forget the photo I sent?" needs an answer.
            logger.info("Reduced old turns to text", extra={"turnIds": reduced})
        return bool(reduced)

    def _drop_oldest_turns(self) -> None:
        """Drop the oldest `_TRUNCATE_PERCENTAGE` of turns (at least one) — nothing cheaper is left."""
        count = max(int(len(self._turns) * _TRUNCATE_PERCENTAGE), 1)
        self._forget({turn.id for turn in self._turns[:count]}, reason=ForgetReason.OVER_BUDGET)

    def _forget(self, turn_ids: set[int], *, reason: ForgetReason) -> int:
        """Remove turns from the active transcript and queue their soft-delete.

        Every removal goes through here — compaction and the agent's `delete_turns` — so all of them
        persist the same way on `flush()` and none can come back on `load()`. A turn already written
        soft-deleted (a pure tool round) simply matches nothing when that update runs.
        """
        if not turn_ids:
            return 0
        before = len(self._turns)
        self._turns = [turn for turn in self._turns if turn.id not in turn_ids]
        self._dropped.update(turn_ids)
        removed = before - len(self._turns)
        logger.info(
            "Turns left the transcript",
            extra={"reason": reason, "turnIds": sorted(turn_ids), "turnsAfter": len(self._turns)},
        )
        return removed

    async def delete_turns(self, turn_ids: list[int]) -> int:
        """Remove whole turns by id — the agent's own `prune_transcript` reaching into its context."""
        return self._forget(set(turn_ids), reason=ForgetReason.AGENT)

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
        # Restored whole, payloads and all: nothing here knows yet how big the context actually is,
        # and cutting on a guess would take the photo out of a conversation nowhere near its budget.
        # The first `compact()` after the first reply has a real measurement and cuts then, if needed.

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

    async def link_messages(self, message_ids: list[int]) -> None:
        """Attach the Telegram messages that delivered the newest turn's answer to that turn.

        The link is only knowable at send time, and it is what lets a later emoji reaction on one of
        those messages be traced back to the turn it graded. Call it AFTER `flush()`: the turn insert
        is fire-and-forget, and this update deliberately does not upsert — a document created here
        would miss the insert's `$setOnInsert` audit fields.
        """
        await self._collection.update_one(
            {"conversation_id": self._conversation_id, "turn_id": self._next_turn_id},
            {"$addToSet": {"message_ids": {"$each": message_ids}}},
        )

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


class TurnLookup:
    """Reverse lookup over `conversation_turns`: a delivered Telegram message → the turn it came from.

    The forward link is written by `MongoMessageHistory.link_messages`. Lifecycle: long-lived — one
    per bot, held by whoever sees Telegram message ids without owning a conversation's history.
    """

    def __init__(self, database: AsyncDatabase) -> None:
        """Bind to the turns collection; the conversation is a query argument, not a scope."""
        self._collection = database[TURNS_COLLECTION]

    async def turn_for_message(self, *, conversation_id: int, message_id: int) -> int | None:
        """The turn a message belongs to, or None.

        None is ordinary: many messages are not an agent answer at all — a transcript echo, a
        `/lists` view, an error notice. A pruned turn keeps its link, so old answers stay resolvable.
        """
        doc = await self._collection.find_one(
            {"conversation_id": conversation_id, "message_ids": message_id},
            {"turn_id": 1},
        )
        return doc["turn_id"] if doc else None
