"""Cloud Tasks queue for inbound Telegram updates (webhook enqueues, worker processes)."""

__all__ = ["TG_UPDATE_QUEUE", "tg_update_queue"]

import pulumi_gcp as gcp

TG_UPDATE_QUEUE = "tg-update-queue"

# Tasks are OIDC-signed as the cloud-run runtime SA (created in iam.py) — no dedicated invoker
# SA. Any run.invoker grant (if ingress is locked down) is manual: IAM is by hand (see CLAUDE.md).
# Pulumi owns only the queue.
tg_update_queue = gcp.cloudtasks.Queue(
    "tg-update-queue",
    name=TG_UPDATE_QUEUE,
    location=gcp.config.region,
    project=gcp.config.project,
    # At-most-once: a failed worker is not retried, so a hard error can't re-run the agent
    # and double-answer (task-name dedup only covers re-delivery, not worker retries).
    retry_config=gcp.cloudtasks.QueueRetryConfigArgs(max_attempts=1),
)
