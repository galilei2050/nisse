"""ScheduleRunner — fires one due task: claim, (re-arm if recurring), run the agent, deliver."""

from __future__ import annotations

from typing import TYPE_CHECKING

from baski.primitives import datetime

from app.scheduling.store import ScheduleKind, claim, mark_done, reschedule

if TYPE_CHECKING:  # break the assistant→scheduling→runner→assistant import cycle (type-only need)
    from aiogram import Bot
    from pymongo.asynchronous.database import AsyncDatabase

    from app.assistant import Assistant
    from app.scheduling.service import SchedulingService


class ScheduleRunner:
    """Executes a due task end-to-end when Cloud Tasks calls the fire endpoint."""

    def __init__(
        self, *, assistant: Assistant, bot: Bot, database: AsyncDatabase, scheduling: SchedulingService
    ) -> None:
        """Hold the collaborators a fire needs: the agent, the Telegram bot, the DB, the enqueuer."""
        self._assistant = assistant
        self._bot = bot
        self._database = database
        self._scheduling = scheduling

    async def fire(self, *, public_id: str, fire_at: datetime.datetime) -> None:
        """Claim the occurrence (idempotent), re-arm a recurring one, run the agent, deliver the reply.

        Advance-then-execute: a recurring task is re-armed and re-enqueued for its next occurrence
        BEFORE the agent runs, so a crash mid-reply can't drop the schedule. A duplicate delivery of
        the same occurrence loses the claim and returns without side effects.
        """
        task = await claim(self._database, public_id=public_id, fire_at=fire_at)
        if task is None:
            return  # duplicate delivery, cancelled, or already advanced — nothing to do

        if task.kind is ScheduleKind.RECURRING:
            if task.repeat_every_hours is None:  # impossible per ScheduledTask's validator — tripwire
                raise RuntimeError(f"recurring task {public_id} has no repeat_every_hours")
            next_fire = self._next_occurrence(fire_at, task.repeat_every_hours)
            await reschedule(self._database, public_id=public_id, fire_at=next_fire)
            await self._scheduling.enqueue_fire(public_id=public_id, fire_at=next_fire)

        answer = await self._assistant.reply(
            conversation_id=task.conversation_id, text=f"[Запланировано] {task.instruction}"
        )
        await self._bot.send_message(chat_id=task.conversation_id, text=answer)

        if task.kind is ScheduleKind.ONCE:
            await mark_done(self._database, public_id=public_id)

    @staticmethod
    def _next_occurrence(fire_at: datetime.datetime, repeat_every_hours: int) -> datetime.datetime:
        """First multiple of the interval strictly after now (skips occurrences missed while down)."""
        step = datetime.timedelta(hours=repeat_every_hours)
        nxt = fire_at + step
        now = datetime.now()
        while nxt <= now:
            nxt += step
        return nxt
