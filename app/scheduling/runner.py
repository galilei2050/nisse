"""ScheduleRunner — fires one due task: claim, (re-arm if recurring), run the agent, deliver."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from baski.primitives import datetime

from app.scheduling.store import SCHEDULED_PREFIX, FireStore, ScheduleKind

if TYPE_CHECKING:  # break the assistant→scheduling→runner→assistant import cycle (type-only need)
    from baski.agents import AgentExecuteResult
    from pymongo.asynchronous.database import AsyncDatabase

    from app.assistant import Assistant
    from app.scheduling.service import SchedulingService
    from app.shared import MessageSender

# How a result becomes the text sent to the owner. Taken as a dependency, not imported: `app.chat`
# imports this package (the /schedules viewer), so importing the chat layer back would cycle.
AnswerFormatter = Callable[["AgentExecuteResult"], str]


class ScheduleRunner:
    """Executes a due task end-to-end when Cloud Tasks calls the fire endpoint.

    Lifecycle: long-lived — built once when the fire route is mounted, serves every fire.
    """

    def __init__(  # noqa: PLR0913 — one collaborator per thing a fire touches
        self,
        *,
        assistant: Assistant,
        sender: MessageSender,
        database: AsyncDatabase,
        scheduling: SchedulingService,
        format_answer: AnswerFormatter,
    ) -> None:
        """Hold the collaborators a fire needs: the agent, the channel, the task store, the enqueuer, the formatter."""
        self._assistant = assistant
        self._sender = sender
        self._tasks = FireStore(database)
        self._scheduling = scheduling
        self._format_answer = format_answer

    async def fire(self, *, public_id: str, fire_at: datetime.datetime) -> None:
        """Claim the occurrence (idempotent), re-arm a recurring one, run the agent, deliver the reply.

        The `claim` context manager guarantees release: if anything below raises, the claim goes back
        to PENDING, so the occurrence is re-armable rather than wedged — nothing re-delivers it on its
        own, since the queue is at-most-once. Advance-then-execute: a recurring task is re-armed
        and re-enqueued for its next occurrence BEFORE the agent runs, so a crash can't drop the schedule.
        A duplicate delivery loses the claim (task is None) and returns without side effects.
        """
        async with self._tasks.claim(public_id=public_id, fire_at=fire_at) as task:
            if task is None:
                return  # duplicate delivery, cancelled, or already advanced — nothing to do

            if task.kind is ScheduleKind.RECURRING:
                if task.repeat_every_hours is None:  # impossible per ScheduledTask's validator — tripwire
                    raise RuntimeError(f"recurring task {public_id} has no repeat_every_hours")
                next_fire = self._next_occurrence(fire_at, task.repeat_every_hours)
                await self._tasks.reschedule(public_id=public_id, fire_at=next_fire)
                await self._scheduling.enqueue_fire(public_id=public_id, fire_at=next_fire)

            try:
                reply = await self._assistant.reply(
                    conversation_id=task.conversation_id, text=f"{SCHEDULED_PREFIX} {task.instruction}"
                )
                await self._sender.send(chat_id=task.conversation_id, text=self._format_answer(reply.result))
                if task.kind is ScheduleKind.ONCE:
                    await self._tasks.mark_done(public_id=public_id)
            finally:
                # Await the reply's fired history writes on every path (mirrors the chat router), so a
                # failed fire never abandons completed turns — the queue never re-delivers them.
                await self._assistant.flush(conversation_id=task.conversation_id)

    @staticmethod
    def _next_occurrence(fire_at: datetime.datetime, repeat_every_hours: int) -> datetime.datetime:
        """First multiple of the interval strictly after now (skips occurrences missed while down)."""
        step = datetime.timedelta(hours=repeat_every_hours)
        nxt = fire_at + step
        now = datetime.now()
        while nxt <= now:
            nxt += step
        return nxt
