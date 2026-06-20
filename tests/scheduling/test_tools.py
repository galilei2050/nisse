"""Scheduling tools + model invariants (no DB): tool logic, past-time rejection, enqueue."""

import pytest
from baski.primitives import datetime
from pydantic import ValidationError

from app.scheduling.dispatch import Scheduling
from app.scheduling.store import ScheduledTask, ScheduleKind
from app.scheduling.tools import RemindTool, RoutineTool


class _FakeScheduler:
    """Records enqueue calls instead of hitting Cloud Tasks."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def enqueue(self, **kwargs) -> bool:  # noqa: ANN003 — matches the Scheduler protocol loosely
        self.calls.append(kwargs)
        return True


class _FakeStore:
    """In-memory stand-in for the conversation-scoped ScheduleStore."""

    def __init__(self) -> None:
        self.added: list[ScheduledTask] = []

    async def add(self, *, kind, instruction, fire_at, repeat_every_hours=None) -> ScheduledTask:  # noqa: ANN001
        task = ScheduledTask(
            conversation_id=1, kind=kind, instruction=instruction, fire_at=fire_at, repeat_every_hours=repeat_every_hours
        )
        self.added.append(task)
        return task


def _scheduling() -> tuple[Scheduling, _FakeScheduler]:
    scheduler = _FakeScheduler()
    return Scheduling(scheduler=scheduler, endpoint="https://x.test/schedule/fire"), scheduler


async def test_remind_stores_once_and_enqueues() -> None:
    store, (scheduling, scheduler) = _FakeStore(), _scheduling()
    fire_at = datetime.now() + datetime.timedelta(hours=2)
    result = await RemindTool(store, scheduling).execute(fire_at=fire_at, instruction="купить молоко")
    assert store.added[0].kind is ScheduleKind.ONCE
    assert store.added[0].repeat_every_hours is None
    assert len(scheduler.calls) == 1
    assert store.added[0].public_id in result


async def test_remind_rejects_past_time_without_storing() -> None:
    store, (scheduling, scheduler) = _FakeStore(), _scheduling()
    past = datetime.now() - datetime.timedelta(minutes=1)
    result = await RemindTool(store, scheduling).execute(fire_at=past, instruction="x")
    assert "past" in result
    assert store.added == []
    assert scheduler.calls == []


async def test_routine_stores_recurring_with_period() -> None:
    store, (scheduling, scheduler) = _FakeStore(), _scheduling()
    fire_at = datetime.now() + datetime.timedelta(hours=1)
    await RoutineTool(store, scheduling).execute(first_fire_at=fire_at, repeat_every_hours=24, instruction="погода")
    assert store.added[0].kind is ScheduleKind.RECURRING
    assert store.added[0].repeat_every_hours == 24
    assert len(scheduler.calls) == 1


def test_recurring_requires_period_and_once_forbids_it() -> None:
    now = datetime.now()
    with pytest.raises(ValidationError):
        ScheduledTask(conversation_id=1, kind=ScheduleKind.RECURRING, instruction="x", fire_at=now)
    with pytest.raises(ValidationError):
        ScheduledTask(conversation_id=1, kind=ScheduleKind.ONCE, instruction="x", fire_at=now, repeat_every_hours=24)
