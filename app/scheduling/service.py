"""SchedulingService + LoggingScheduler — the scheduling seam a tool sees, and a no-Cloud-Tasks stand-in."""

import json
import logging

from baski.clients.scheduler import Scheduler
from baski.primitives import datetime

logger = logging.getLogger(__name__)

# Cloud Tasks waits at most this long for /schedule/fire to answer; an agent reply fits well inside.
_DISPATCH_DEADLINE = datetime.timedelta(minutes=10)


class LoggingScheduler:
    """A `Scheduler` that logs instead of enqueuing — used in polling/probe where there's no Cloud Tasks.

    Lifecycle: long-lived, one per process (held in CoreDeps). Lets scheduling tools exist (and be
    tested locally — the agent's `remind` call lands in the log) without a real queue or callback.
    """

    async def enqueue(  # noqa: PLR0913 — mirrors CloudTasksScheduler's signature so it's a drop-in Scheduler
        self,
        *,
        endpoint: str,
        task_name: str,
        payload: bytes,
        dispatch_deadline: datetime.timedelta,
        headers: dict[str, str] | None = None,
        schedule_time: datetime.datetime | None = None,
    ) -> bool:
        """Log the task that would have been enqueued (no Cloud Tasks); always reports success."""
        logger.info(
            "Scheduler (no Cloud Tasks): would enqueue",
            extra={
                "cloudTaskName": task_name,  # NOT `taskName` — LogRecord owns that name since 3.12
                "scheduleTime": str(schedule_time),
                "endpoint": endpoint,
                "payloadBytes": len(payload),
                "deadline": str(dispatch_deadline),
                "hasHeaders": headers is not None,
            },
        )
        return True


class SchedulingService:
    """Schedules a task's fire on Cloud Tasks. Tools and the runner talk to this — never the raw scheduler.

    Lifecycle: short-lived glue, assembled on the stack wherever it's needed (per conversation for the
    tools, once for the fire runner). Hides the transport (callback endpoint, task naming, payload,
    deadline) behind one method, so a tool depends on a narrow domain service, not on the scheduler/URL.
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
