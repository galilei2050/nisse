"""Secret Manager containers + IAM bindings for nisse runtime."""

import pulumi_gcp as gcp
from iam import cloud_run_service_account

telegram_token = gcp.secretmanager.Secret(
    "telegram-token",
    secret_id="TELEGRAM_TOKEN",
    replication=gcp.secretmanager.SecretReplicationArgs(auto=gcp.secretmanager.SecretReplicationAutoArgs()),
)

# Cloud Run runtime SA can read the token value.
gcp.secretmanager.SecretIamMember(
    "telegram-token-accessor",
    secret_id=telegram_token.id,
    role="roles/secretmanager.secretAccessor",
    member=cloud_run_service_account.email.apply(lambda e: f"serviceAccount:{e}"),
)
