"""Enqueue a task's fire onto Cloud Tasks — shared by the create tools and the recurring re-arm."""

import json
from dataclasses import dataclass

from baski.clients.scheduler import Scheduler
from baski.primitives import datetime

# Cloud Tasks waits at most this long for /schedule/fire to answer; an agent reply fits well inside.
_DISPATCH_DEADLINE = datetime.timedelta(minutes=10)


@dataclass(frozen=True)
class Scheduling:
    """Cloud Tasks wiring for self-invocation: the enqueuer + the public URL it calls back."""

    scheduler: Scheduler  # baski's Cloud Tasks enqueuer (reused, not reinvented)
    endpoint: str  # full URL of the /schedule/fire worker


async def enqueue_fire(scheduling: Scheduling, *, public_id: str, fire_at: datetime.datetime) -> None:
    """Schedule one Cloud Task to POST {public_id, fire_at} to the fire endpoint at fire_at.

    `task_name` is deterministic per occurrence (public_id + epoch), so a duplicate enqueue of the
    same occurrence is deduped at the queue (baski returns False on AlreadyExists).
    """
    payload = json.dumps({"public_id": public_id, "fire_at": fire_at.isoformat()}).encode()
    await scheduling.scheduler.enqueue(
        endpoint=scheduling.endpoint,
        task_name=f"sched-{public_id}-{int(fire_at.timestamp())}",
        payload=payload,
        dispatch_deadline=_DISPATCH_DEADLINE,
        schedule_time=fire_at,
    )
