"""Service accounts for nisse runtime."""

import pulumi_gcp as gcp

cloud_run_service_account = gcp.serviceaccount.Account(
    "cloud-run",
    account_id="cloud-run",
    display_name="Cloud Run runtime SA — nisse backend",
)
