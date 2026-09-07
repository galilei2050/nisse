"""A schedule whose moment has passed must read as missed — to the owner and to the agent.

The owner's morning routine stopped firing on 2026-07-04 and both surfaces kept calling it armed
("next 2026-07-04 15:00Z") for 34 days, so asked "is it still on?", the bot answered yes.
"""

from baski.primitives import datetime

from app.chat.saved import _render_schedule
from app.scheduling.store import ScheduledTask, ScheduleKind, ScheduleStatus
from app.scheduling.tools import CancelScheduleTool

# The renderers read the clock themselves, so the cases are offsets from the real now.
_NOW = datetime.now()


def _task(*, fire_at: datetime.datetime, status: ScheduleStatus = ScheduleStatus.PENDING) -> ScheduledTask:
    return ScheduledTask(
        public_id="6ca1cff154",
        conversation_id=1,
        kind=ScheduleKind.RECURRING,
        instruction="Утренний чек-ин",
        fire_at=fire_at,
        repeat_every_hours=24,
        status=status,
    )


class _FakeStore:
    """Stands in for ScheduleStore.list() — the tool reads the armed tasks live each turn."""

    def __init__(self, tasks: list[ScheduledTask]) -> None:
        self._tasks = tasks

    async def list(self) -> list[ScheduledTask]:
        return self._tasks


def test_a_passed_occurrence_is_overdue():
    assert _task(fire_at=_NOW - datetime.timedelta(days=34)).is_overdue(_NOW)
    assert not _task(fire_at=_NOW + datetime.timedelta(hours=1)).is_overdue(_NOW)
    # Already claimed and running is not "missed" — it is in flight.
    assert not _task(fire_at=_NOW - datetime.timedelta(days=1), status=ScheduleStatus.RUNNING).is_overdue(_NOW)


def test_the_owner_is_not_told_a_dead_routine_is_armed():
    missed = _render_schedule(_task(fire_at=_NOW - datetime.timedelta(days=34)))
    assert "не сработало" in missed
    assert "⏰" not in missed  # the armed-clock face would read as a promise it cannot keep
    armed = _render_schedule(_task(fire_at=_NOW + datetime.timedelta(hours=1)))
    assert armed.startswith("⏰")


async def test_the_agent_is_not_told_a_dead_routine_is_armed():
    """This block goes into the model's context every turn — it is where "yes, it's on" came from."""
    tool = CancelScheduleTool(_FakeStore([_task(fire_at=_NOW - datetime.timedelta(days=34))]))  # type: ignore[arg-type]

    message = await tool.user_message()

    assert message is not None
    text = message["content"][0]["text"]
    assert "MISSED" in text
    assert "next" not in text
