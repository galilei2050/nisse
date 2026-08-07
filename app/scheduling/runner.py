"""ScheduleRunner — fires one due task: claim, (re-arm if recurring), run the agent, deliver.

It also owns the sweep: the periodic reader that finds occurrences the queue never delivered, so a
schedule's life stops depending on a single at-most-once message.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from baski.primitives import datetime

from app.scheduling.store import SCHEDULED_PREFIX, FireStore, ScheduledTask, ScheduleKind

logger = logging.getLogger(__name__)

# How many stranded occurrences one sweep handles. Each one-shot costs a full agent run, and the
# service holds a single instance, so an unbounded batch would park the owner's chat behind it.
_SWEEP_BATCH = 10
# Appended to a late reminder's instruction so the agent can say so instead of acting as if on time.
_LATE_NOTE = " [опоздание: должно было прийти {when} UTC]"

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

    async def sweep(self) -> int:
        """Repair every occurrence whose moment passed without firing. Returns how many were handled.

        A task is armed by one at-most-once queue message, so a dispatch that never lands leaves the
        row PENDING at a past `fire_at` forever with nothing to notice — the owner's morning routine
        stood still for 34 days that way. The durable row is the trigger of record; this is the reader
        that makes it one, on a clock that does not depend on the queue.

        A missed routine is moved to its next occurrence and not replayed — a morning check-in is not
        worth having at midnight. A missed one-shot IS delivered, however late, marked as late.
        """
        due = await self._tasks.due(now=datetime.now(), limit=_SWEEP_BATCH)
        for task in due:
            if task.kind is ScheduleKind.RECURRING:
                await self._rearm(task)
            else:
                await self._deliver_late(task)
        logger.info("Schedule sweep finished", extra={"handled": len(due)})
        return len(due)

    async def _deliver_late(self, task: ScheduledTask) -> None:
        """Give a stranded one-shot exactly one attempt, then close it however it went.

        Deliberate degrade, not a swallowed error: a failed fire releases its claim back to PENDING,
        so leaving it open would hand the same task back to every following sweep — and the usual
        reason delivery fails here is a chat that no longer exists, which no repeat can fix. Each
        attempt costs a full agent run, so the retry loop would bill for a reminder nobody can
        receive. One attempt, then the task is done and the failure is loud.
        """
        try:
            await self.fire(public_id=task.public_id, fire_at=task.fire_at, late=True)
        except Exception:
            logger.exception("Dropped a reminder that could not be delivered", extra={"publicId": task.public_id})
            await self._tasks.mark_done(public_id=task.public_id)

    async def _rearm(self, task: ScheduledTask) -> None:
        """Point a stranded routine at its next future occurrence and queue that one."""
        if task.repeat_every_hours is None:  # impossible per ScheduledTask's validator — tripwire
            raise RuntimeError(f"recurring task {task.public_id} has no repeat_every_hours")
        next_fire = self._next_occurrence(task.fire_at, task.repeat_every_hours)
        await self._tasks.reschedule(public_id=task.public_id, fire_at=next_fire)
        await self._scheduling.enqueue_fire(public_id=task.public_id, fire_at=next_fire)
        logger.warning(
            "Re-armed a routine that never fired",
            extra={"publicId": task.public_id, "missedAt": task.fire_at.isoformat(), "nextAt": next_fire.isoformat()},
        )

    async def fire(self, *, public_id: str, fire_at: datetime.datetime, late: bool = False) -> None:
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

            # A late reminder still gets delivered, and says it is late: silently arriving days after
            # its moment reads as the bot being confused rather than as the bot catching up.
            overdue = _LATE_NOTE.format(when=fire_at.strftime("%d.%m %H:%M")) if late else ""
            try:
                reply = await self._assistant.reply(
                    conversation_id=task.conversation_id, text=f"{SCHEDULED_PREFIX}{overdue} {task.instruction}"
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
