"""Cloud Scheduler job that repairs schedules the task queue never delivered.

A scheduled task is armed by a single Cloud Tasks message and the queue is at-most-once, so one
dispatch that does not land leaves the row PENDING at a past moment forever — the owner's morning
routine went 34 days that way, while `/schedules` kept calling it armed. This job makes the durable
row the trigger of record: every 15 minutes the backend re-reads what is overdue, on a clock that
does not depend on the queue.

Fifteen minutes is deliberately coarse. Cloud Tasks remains the fast path (measured ~150ms, and it
has delivered every occurrence it carried); this is the net underneath it, so a wide interval costs
nothing but a few minutes of lateness on the rare occurrence the net has to catch, and avoids paying
a cold start every minute for a check that almost always finds nothing.
"""

__all__ = ["schedule_sweep"]

import pulumi_gcp as gcp
from iam import cloud_run_service_account

from .cloud_run_backend import backend_cloud_run_service

SWEEP_SCHEDULE = "*/15 * * * *"

schedule_sweep = gcp.cloudscheduler.Job(
    "schedule-sweep",
    name="schedule-sweep",
    description="Re-arm routines and deliver reminders whose Cloud Tasks dispatch never landed",
    schedule=SWEEP_SCHEDULE,
    time_zone="Etc/UTC",  # a fixed-interval job has no local meaning; UTC keeps it DST-proof
    attempt_deadline="1800s",  # a stranded one-shot costs a full agent run, and a batch holds up to 10
    # Retrying would re-deliver reminders the first attempt already sent. The next tick is 15 minutes
    # away and re-reads the same rows, so a skipped run repairs itself.
    retry_config=gcp.cloudscheduler.JobRetryConfigArgs(retry_count=0),
    http_target=gcp.cloudscheduler.JobHttpTargetArgs(
        uri=backend_cloud_run_service.uri.apply(lambda uri: f"{uri}/schedule/sweep"),
        http_method="POST",
        oidc_token=gcp.cloudscheduler.JobHttpTargetOidcTokenArgs(
            service_account_email=cloud_run_service_account.email,
        ),
    ),
)
