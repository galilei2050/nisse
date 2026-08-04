"""Cloud Scheduler job that runs the nightly curator.

POSTs an empty body to `/curate`, which the route reads as "every conversation with recent traffic".
Signed as the same `cloud-run@` runtime SA the Cloud Tasks worker uses — the service is
`allow_unauthenticated` today, so the OIDC token is what keeps the call authenticated if ingress is
ever locked down.

Time is deliberate: 04:00 America/Los_Angeles is after the owner is asleep and before the morning
check-in, so the assistant starts the day already carrying yesterday's lessons and the report is the
first thing waiting.
"""

__all__ = ["curator_schedule"]

import pulumi_gcp as gcp
from iam import cloud_run_service_account

from .cloud_run_backend import backend_cloud_run_service

CURATE_SCHEDULE = "0 4 * * *"
CURATE_TIMEZONE = "America/Los_Angeles"

curator_schedule = gcp.cloudscheduler.Job(
    "curator-nightly",
    name="curator-nightly",
    description="Nightly memory consolidation pass over every active conversation",
    schedule=CURATE_SCHEDULE,
    time_zone=CURATE_TIMEZONE,
    # One pass reviews a day and edits the stores; a retry would re-run edits the first attempt
    # already made, against a window it has already learned from. A missed night is cheaper.
    attempt_deadline="1800s",
    retry_config=gcp.cloudscheduler.JobRetryConfigArgs(retry_count=0),
    http_target=gcp.cloudscheduler.JobHttpTargetArgs(
        uri=backend_cloud_run_service.uri.apply(lambda uri: f"{uri}/curate"),
        http_method="POST",
        oidc_token=gcp.cloudscheduler.JobHttpTargetOidcTokenArgs(
            service_account_email=cloud_run_service_account.email,
        ),
    ),
)
