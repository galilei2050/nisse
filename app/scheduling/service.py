"""SchedulingService — the only scheduling seam a tool sees; hides the Cloud Tasks transport."""

import json

from baski.clients.scheduler import Scheduler
from baski.primitives import datetime

# Cloud Tasks waits at most this long for /schedule/fire to answer; an agent reply fits well inside.
_DISPATCH_DEADLINE = datetime.timedelta(minutes=10)


class SchedulingService:
    """Schedules a task's fire on Cloud Tasks. Tools and the runner talk to this — never the raw scheduler.

    Hides the transport (callback endpoint, task naming, payload, deadline) behind one method, so a
    tool depends on a narrow domain service, not on baski's scheduler or the callback URL.
    """

    def __init__(self, *, scheduler: Scheduler, endpoint: str) -> None:
        """Bind the Cloud Tasks enqueuer and the public URL it calls back."""
        self._scheduler = scheduler
        self._endpoint = endpoint

    async def enqueue_fire(self, *, public_id: str, fire_at: datetime.datetime) -> None:
        """Schedule one Cloud Task to POST {public_id, fire_at} to the fire endpoint at fire_at.

        `task_name` is deterministic per occurrence (public_id + epoch), so a duplicate enqueue of the
        same occurrence is deduped at the queue (baski returns False on AlreadyExists).
        """
        payload = json.dumps({"public_id": public_id, "fire_at": fire_at.isoformat()}).encode()
        await self._scheduler.enqueue(
            endpoint=self._endpoint,
            task_name=f"sched-{public_id}-{int(fire_at.timestamp())}",
            payload=payload,
            dispatch_deadline=_DISPATCH_DEADLINE,
            schedule_time=fire_at,
        )
