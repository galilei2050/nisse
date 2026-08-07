"""Conversation — one chat's reused agent and the per-reply loop that drives it."""

import asyncio
from typing import NamedTuple

from baski.agents import Agent, AgentExecuteResult, Listener, noop
from baski.agents.tools import ShortTermMemory

from app.assistant.history import MongoMessageHistory
from app.shared.blocks import Media, MediaType


class Reply(NamedTuple):
    """One finished reply: the agent's raw result, and the transcript turn its answer landed in.

    The turn id travels with the result because it is only correct at the moment the reply ends —
    see `MongoMessageHistory.link_messages`.
    """

    result: AgentExecuteResult
    turn_id: int


def _continued(first: AgentExecuteResult, second: AgentExecuteResult) -> AgentExecuteResult:
    """Two passes of the same reply as one result: the later answer, the spend and verdicts of both.

    The owner saw a single growing message and is owed a single, true cost line under it.
    """
    return second.model_copy(
        update={
            "total_input_tokens": first.total_input_tokens + second.total_input_tokens,
            "total_output_tokens": first.total_output_tokens + second.total_output_tokens,
            "turn_count": first.turn_count + second.turn_count,
            "tool_call_count": first.tool_call_count + second.tool_call_count,
            "total_cost": first.total_cost + second.total_cost,
            "judge_verdicts": [*first.judge_verdicts, *second.judge_verdicts],
        }
    )


class Conversation:
    """One chat's long-lived agent (+ history, scratchpad) and the orchestration to run one reply.

    Lifecycle: long-lived — one per conversation, built once and reused across every reply (baski's
    loop preserves context across `execute()` calls). The lock serializes replies, since the agent's
    history is shared mutable state.
    """

    def __init__(self, *, agent: Agent, history: MongoMessageHistory, short_term: ShortTermMemory) -> None:
        """Hold the long-lived collaborators and create the per-conversation reply lock."""
        self._agent = agent
        self._history = history
        self._short_term = short_term
        self._lock = asyncio.Lock()
        self._running = False  # the agent loop is driving; a message handed over now still gets read

    async def reply(self, *, text: str, media: Media | None = None, on_event: Listener = noop) -> Reply:
        """Run one reply over the reused agent: reset the scratchpad, append the message, drive the loop.

        `media` is a photo/PDF the user attached (None for a text/voice turn) — added as its own user
        message, with the caption text as a second one. `short_term` is a per-reply scratchpad, cleared
        each time; `on_event` is the fresh live-progress listener. Serialized so two replies never drive
        the one agent's history at once.

        The loop runs again if a message was `deliver`ed too late for it — after its last turn was
        built, while it was writing the answer or being graded. Everything delivered before that is
        already in the transcript the loop is reading, so it is answered without a second pass.
        """
        async with self._lock:
            self._short_term.clear()
            self._agent.on_event = on_event
            with self._history:
                if media is not None:
                    if media.media_type is MediaType.PDF:
                        self._history.add_document(data=media.data)
                    else:
                        self._history.add_photo(data=media.data, media_type=media.media_type)
                if text:
                    self._history.add_user_text(text)
            self._running = True
            try:
                result = await self._agent.execute()
                while self._history.has_incoming:
                    result = _continued(result, await self._agent.execute())
            finally:
                self._running = False
            return Reply(result=result, turn_id=self._history.last_turn_id)

    def deliver(self, text: str) -> bool:
        """Hand a message to the reply already running; False if none is, so the caller starts one.

        Deliberately not async: nothing may await between the check and the hand-off. The reply
        clears `_running` without awaiting once its loop has stopped reading the transcript, so a
        message accepted here is always still picked up — either by the turn being built, or by the
        extra pass `reply` runs for one that arrived after the last turn.
        """
        if not self._running:
            return False
        self._history.deliver(text)
        return True

    async def flush(self) -> None:
        """Await the reply's durable history writes. Called after the answer is sent to the user.

        Under the same lock as `reply()` so it never races a concurrent reply mutating the history's
        write/drop bookkeeping — the answer is already sent, so this waits off the user's critical path.
        """
        async with self._lock:
            await self._history.flush()

    async def link_messages(self, *, turn_id: int, message_ids: list[int]) -> None:
        """Record which Telegram messages carried a reply's answer. Runs after `flush()`.

        No lock: the turn is named outright, so this touches none of the in-memory bookkeeping a
        concurrent reply is mutating — and waiting for that reply would only delay a write the owner
        is already past.
        """
        await self._history.link_messages(turn_id=turn_id, message_ids=message_ids)
