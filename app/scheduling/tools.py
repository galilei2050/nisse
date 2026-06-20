"""The agent's scheduling tools: a one-shot `remind` and a recurring `schedule_routine`."""

from baski.agents.tool import Tool
from baski.primitives import datetime
from pydantic import BaseModel, Field

from app.scheduling.dispatch import Scheduling, enqueue_fire
from app.scheduling.store import ScheduleKind, ScheduleStore

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
    """Schedule a one-shot reminder to fire at a future time."""

    name = "remind"
    one_line = "Schedule a one-shot reminder at a future UTC time"
    description = "Fire a reminder to the owner once, at a future time."

    class Input(BaseModel):
        """Arguments for a one-shot reminder."""

        fire_at: datetime.datetime = Field(description="When to fire, absolute UTC (ISO-8601)")
        instruction: str = Field(description="What to tell/do for the owner when it fires")

    def __init__(self, store: ScheduleStore, scheduling: Scheduling) -> None:
        """Hold the conversation-scoped store and the Cloud Tasks enqueuer."""
        self._store = store
        self._scheduling = scheduling

    async def execute(self, *, fire_at: datetime.datetime, instruction: str) -> str:
        """Persist the task and enqueue its fire; reject a time in the past."""
        fire_at = _normalize(fire_at)
        if fire_at <= datetime.now():
            return "fire_at is in the past — pass a future UTC time."
        task = await self._store.add(kind=ScheduleKind.ONCE, instruction=instruction, fire_at=fire_at)
        await enqueue_fire(self._scheduling, public_id=task.public_id, fire_at=fire_at)
        return f"Reminder {task.public_id} set for {fire_at.isoformat()}."

    def system_prompt(self) -> str:
        """When/how to schedule a one-shot reminder."""
        return f"Use remind for a one-time future reminder. {_TIME_GUIDANCE}"


class RoutineTool(Tool):
    """Schedule a recurring routine that repeats on a fixed hourly interval."""

    name = "schedule_routine"
    one_line = "Schedule a recurring routine (e.g. every morning)"
    description = "Fire a routine to the owner repeatedly, every N hours, starting at a future time."

    class Input(BaseModel):
        """Arguments for a recurring routine."""

        first_fire_at: datetime.datetime = Field(description="First occurrence, absolute UTC (ISO-8601)")
        repeat_every_hours: int = Field(description="Interval between fires in hours: 24=daily, 168=weekly", gt=0)
        instruction: str = Field(description="What to tell/do for the owner each time it fires")

    def __init__(self, store: ScheduleStore, scheduling: Scheduling) -> None:
        """Hold the conversation-scoped store and the Cloud Tasks enqueuer."""
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
        await enqueue_fire(self._scheduling, public_id=task.public_id, fire_at=first_fire_at)
        return f"Routine {task.public_id} set, every {repeat_every_hours}h from {first_fire_at.isoformat()}."

    def system_prompt(self) -> str:
        """When/how to schedule a recurring routine."""
        return f"Use schedule_routine for a repeating routine (24h=daily). {_TIME_GUIDANCE}"
