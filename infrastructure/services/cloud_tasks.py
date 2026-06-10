"""Cloud Tasks queue for inbound Telegram updates (webhook enqueues, worker processes)."""

__all__ = ["cloud_tasks_invoker_sa", "tg_update_queue"]

import pulumi_gcp as gcp

from .cloud_run_backend import backend_cloud_run_service

cloud_tasks_invoker_sa = gcp.serviceaccount.Account(
    "cloud-tasks-invoker-sa",
    account_id="cloud-tasks-invoker",
    display_name="Cloud Tasks Invoker — nisse webhook worker",
    project=gcp.config.project,
)

tg_update_queue = gcp.cloudtasks.Queue(
    "tg-update-queue",
    name="tg-update-queue",
    location=gcp.config.region,
    project=gcp.config.project,
    # One agent run at a time — the backend runs at max_instances=1.
    rate_limits=gcp.cloudtasks.QueueRateLimitsArgs(max_concurrent_dispatches=1),
    # At-most-once: a failed worker is not retried, so a hard error can't double-answer.
    retry_config=gcp.cloudtasks.QueueRetryConfigArgs(max_attempts=1),
)

# Backend is allow_unauthenticated, so this isn't strictly required today; bind it for
# least-privilege intent and so the OIDC-signed task still works if ingress is locked down.
gcp.cloudrunv2.ServiceIamMember(
    "cloud-tasks-invoker-run-invoker",
    name=backend_cloud_run_service.name,
    location=backend_cloud_run_service.location,
    role="roles/run.invoker",
    member=cloud_tasks_invoker_sa.email.apply(lambda email: f"serviceAccount:{email}"),
)
