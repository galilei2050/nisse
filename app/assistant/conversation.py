"""Conversation — one chat's reused agent and the per-reply loop that drives it."""

import asyncio

from baski.agents import Agent, AgentExecuteResult, Listener, noop
from baski.agents.tools import ShortTermMemory

from app.assistant.history import MongoMessageHistory
from app.shared.blocks import Media, MediaType


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

    async def reply(self, *, text: str, media: Media | None = None, on_event: Listener = noop) -> AgentExecuteResult:
        """Run one reply over the reused agent: reset the scratchpad, append the message, drive the loop.

        `media` is a photo/PDF the user attached (None for a text/voice turn) — added as its own user
        message, with the caption text as a second one. `short_term` is a per-reply scratchpad, cleared
        each time; `on_event` is the fresh live-progress listener. Serialized so two replies never drive
        the one agent's history at once.
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
            return await self._agent.execute()

    async def flush(self) -> None:
        """Await the reply's durable history writes. Called after the answer is sent to the user.

        Under the same lock as `reply()` so it never races a concurrent reply mutating the history's
        write/drop bookkeeping — the answer is already sent, so this waits off the user's critical path.
        """
        async with self._lock:
            await self._history.flush()

    async def link_messages(self, message_ids: list[int]) -> None:
        """Record which Telegram messages carried this reply's answer. Runs after `flush()`."""
        async with self._lock:
            await self._history.link_messages(message_ids)
