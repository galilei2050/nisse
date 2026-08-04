"""SchedulingService: what one occurrence turns into on the way to the queue.

The task name is the only thing standing between Cloud Tasks' at-least-once delivery and a reminder
that fires twice — it must be derived from the occurrence, never from wall-clock. And the polling /
probe path runs a different `Scheduler` than prod does, so it is exercised here: that stand-in is
what `make probe` drives, and a fault in it looks to the owner like `remind` being broken.
"""

from typing import Any

from baski.primitives import datetime

from app.scheduling.service import LoggingScheduler, SchedulingService

FIRE_AT = datetime.as_utc(datetime.datetime(2026, 8, 5, 17, 0))


class _RecordingScheduler:
    """Captures the one enqueue call, so the test can assert what the queue would have received."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enqueue(self, **kwargs: Any) -> bool:
        """Record the enqueue and report success, as a real scheduler does for a new task."""
        self.calls.append(kwargs)
        return True


async def test_the_task_name_is_derived_from_the_occurrence_so_a_redelivery_dedupes() -> None:
    """Two enqueues of the SAME occurrence must produce the same task name — that name is what the
    queue dedupes on. Deriving it from anything else (a uuid, `now()`) would let one reminder fire
    twice, which is the failure the owner notices and the queue cannot undo."""
    scheduler = _RecordingScheduler()
    service = SchedulingService(scheduler=scheduler, endpoint="http://localhost/schedule/fire")

    await service.enqueue_fire(public_id="abc123", fire_at=FIRE_AT)
    await service.enqueue_fire(public_id="abc123", fire_at=FIRE_AT)

    first, second = scheduler.calls
    assert first["task_name"] == second["task_name"] == f"sched-abc123-{int(FIRE_AT.timestamp())}"
    assert first["schedule_time"] == FIRE_AT  # the queue holds the task until then; no poller exists
    assert first["endpoint"] == "http://localhost/schedule/fire"
    assert first["payload"] == b'{"public_id": "abc123", "fire_at": "2026-08-05T17:00:00+00:00"}'


async def test_a_later_occurrence_of_the_same_task_gets_its_own_name() -> None:
    """A recurring task re-enqueues itself for the next occurrence. Sharing the previous name would
    be swallowed by the queue's dedup and the routine would silently stop after one fire."""
    scheduler = _RecordingScheduler()
    service = SchedulingService(scheduler=scheduler, endpoint="http://localhost/schedule/fire")

    await service.enqueue_fire(public_id="abc123", fire_at=FIRE_AT)
    await service.enqueue_fire(public_id="abc123", fire_at=FIRE_AT + datetime.timedelta(hours=24))

    assert scheduler.calls[0]["task_name"] != scheduler.calls[1]["task_name"]


async def test_the_no_cloud_tasks_stand_in_accepts_a_fire() -> None:
    """`make probe` and local polling run this scheduler, so `remind` only works there if it does.

    It logs the enqueue, and building that log record is the assertion that matters: a field name
    colliding with a reserved `LogRecord` attribute raises inside `logging`, AFTER the task was
    stored — the agent then reports a failed reminder that actually exists. (pytest runs at
    `log_level = INFO`, so the record really is built here.)
    """
    service = SchedulingService(scheduler=LoggingScheduler(), endpoint="http://localhost/schedule/fire")

    await service.enqueue_fire(public_id="abc123", fire_at=FIRE_AT)
