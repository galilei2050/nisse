"""The agent's scheduling tools: create (`remind`/`schedule_routine`) and `cancel_schedule`."""

from anthropic.types import MessageParam, TextBlockParam
from baski.agents.tool import Tool
from baski.primitives import datetime
from pydantic import BaseModel, Field

from app.scheduling.service import SchedulingService
from app.scheduling.store import ScheduleKind, ScheduleStore
from app.shared import CoreDeps
from app.tools.registry import ToolRegistrar

# One line of the guidance the owner asked for: derive the timezone from the owner, don't guess it.
_TIME_GUIDANCE = (
    "Times are absolute UTC — convert from the owner's local time yourself. If you don't know the "
    "owner's city/timezone, ask, then remember it so you don't ask again."
)


def _normalize(when: datetime.datetime) -> datetime.datetime:
    """UTC at second precision.

    Seconds (not micros) so the stored `fire_at` and the Cloud Tasks payload round-trip identically
    through Mongo's millisecond datetimes — the claim matches on it exactly.
    """
    return datetime.as_utc(when).replace(microsecond=0)


class RemindTool(Tool):
    """Schedule a one-shot reminder. Lifecycle: per-conversation (in its toolset)."""

    name = "remind"
    one_line = "Schedule a one-shot reminder at a future UTC time"
    description = "Fire a reminder to the owner once, at a future time."

    class Input(BaseModel):
        """Arguments for a one-shot reminder."""

        fire_at: datetime.datetime = Field(description="When to fire, absolute UTC (ISO-8601)")
        instruction: str = Field(description="What to tell/do for the owner when it fires")

    def __init__(self, store: ScheduleStore, scheduling: SchedulingService) -> None:
        """Hold the conversation-scoped store and the scheduling service."""
        self._store = store
        self._scheduling = scheduling

    async def execute(self, *, fire_at: datetime.datetime, instruction: str) -> str:
        """Persist the task and enqueue its fire; reject a time in the past."""
        fire_at = _normalize(fire_at)
        if fire_at <= datetime.now():
            return "fire_at is in the past — pass a future UTC time."
        task = await self._store.add(kind=ScheduleKind.ONCE, instruction=instruction, fire_at=fire_at)
        await self._scheduling.enqueue_fire(public_id=task.public_id, fire_at=fire_at)
        return f"Reminder {task.public_id} set for {fire_at.isoformat()}."

    async def system_prompt(self) -> str:
        """When/how to schedule a one-shot reminder."""
        return f"Use remind for a one-time future reminder. {_TIME_GUIDANCE}"


class RoutineTool(Tool):
    """Schedule a recurring routine. Lifecycle: per-conversation (in its toolset)."""

    name = "schedule_routine"
    one_line = "Schedule a recurring routine (e.g. every morning)"
    description = "Fire a routine to the owner repeatedly, every N hours, starting at a future time."

    class Input(BaseModel):
        """Arguments for a recurring routine."""

        first_fire_at: datetime.datetime = Field(description="First occurrence, absolute UTC (ISO-8601)")
        repeat_every_hours: int = Field(description="Interval between fires in hours: 24=daily, 168=weekly", gt=0)
        instruction: str = Field(description="What to tell/do for the owner each time it fires")

    def __init__(self, store: ScheduleStore, scheduling: SchedulingService) -> None:
        """Hold the conversation-scoped store and the scheduling service."""
        self._store = store
        self._scheduling = scheduling

    async def execute(self, *, first_fire_at: datetime.datetime, repeat_every_hours: int, instruction: str) -> str:
        """Persist the recurring task and enqueue its first fire; reject a start in the past."""
        first_fire_at = _normalize(first_fire_at)
        if first_fire_at <= datetime.now():
            return "first_fire_at is in the past — pass a future UTC time."
        task = await self._store.add(
            kind=ScheduleKind.RECURRING,
            instruction=instruction,
            fire_at=first_fire_at,
            repeat_every_hours=repeat_every_hours,
        )
        await self._scheduling.enqueue_fire(public_id=task.public_id, fire_at=first_fire_at)
        return f"Routine {task.public_id} set, every {repeat_every_hours}h from {first_fire_at.isoformat()}."

    async def system_prompt(self) -> str:
        """When/how to schedule a recurring routine."""
        return f"Use schedule_routine for a repeating routine (24h=daily). {_TIME_GUIDANCE}"


_SCHEDULES_HEADER = "YOUR ACTIVE SCHEDULES — cancel one with cancel_schedule(public_id):"


class CancelScheduleTool(Tool):
    """Cancel a schedule + inject the active-schedule list. Lifecycle: per-conversation (in its toolset)."""

    name = "cancel_schedule"
    one_line = "Cancel a scheduled reminder or recurring routine by id"
    description = "Stop a scheduled reminder/routine so it no longer fires (a recurring one stops repeating)."

    class Input(BaseModel):
        """Argument for cancelling one schedule."""

        public_id: str = Field(description="The id shown in [brackets] in your active schedules")

    def __init__(self, store: ScheduleStore) -> None:
        """Hold the conversation-scoped store; the active list is read live from it each turn."""
        self._store = store

    async def execute(self, *, public_id: str) -> str:
        """Cancel the schedule and confirm; recurring ones stop after this."""
        return f"Cancelled {public_id}." if await self._store.cancel(public_id) else f"No active schedule {public_id}."

    async def user_message(self) -> MessageParam | None:
        """The always-injected list of still-armed schedules, read live so cancellations show up."""
        tasks = await self._store.list()
        if not tasks:
            return None
        lines = [_SCHEDULES_HEADER]
        for t in tasks:
            every = f" · every {t.repeat_every_hours}h" if t.repeat_every_hours else ""
            when = t.fire_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"- [{t.public_id}] {t.kind}{every} · next {when}Z — {t.instruction}")
        return MessageParam(role="user", content=[TextBlockParam(type="text", text="\n".join(lines))])


def scheduling_tools(deps: CoreDeps, conversation_id: int) -> list[Tool]:
    """Reminders/routines — the scheduling service + a chat-scoped store, their three tools."""
    service = SchedulingService(scheduler=deps.scheduler, endpoint=deps.schedule_endpoint)
    store = ScheduleStore(deps.database, conversation_id=conversation_id)
    return [RemindTool(store, service), RoutineTool(store, service), CancelScheduleTool(store)]


def register_tools(registrar: ToolRegistrar) -> None:
    """Register the reminder/routine tools under the name the main agent references."""
    registrar.register("scheduling", scheduling_tools)
