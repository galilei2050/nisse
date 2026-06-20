"""Conversation — one chat's reused agent and the per-reply loop that drives it."""

import asyncio

from baski.agents import Agent, AgentExecuteResult, Listener, noop
from baski.agents.tools import ShortTermMemory

from app.assistant.history import MongoMessageHistory


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

    async def reply(self, *, text: str, on_event: Listener = noop) -> AgentExecuteResult:
        """Run one reply over the reused agent: reset the scratchpad, append the message, drive the loop.

        `short_term` is a per-reply scratchpad, cleared each time; `on_event` is the fresh
        live-progress listener for this message. Serialized so two replies never drive the one
        agent's history at once.
        """
        async with self._lock:
            self._short_term.clear()
            self._agent.on_event = on_event
            with self._history:
                self._history.add_user_text(text)
            result = await self._agent.execute()
            await self._history.save()
        return result
