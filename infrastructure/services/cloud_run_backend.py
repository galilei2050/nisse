"""Cloud Run service for the nisse Telegram backend."""

import pulumi_gcp as gcp
from baski.infra.run import (
    CloudRunServiceConfig,
    create_cloud_run_secret_env,
    create_cloud_run_with_monitoring,
    repo_short_sha,
)
from delivery import docker_repository_url
from iam import cloud_run_service_account

# Each secret + a version is created and granted to the cloud-run SA manually (outside Pulumi).
telegram_token_env = create_cloud_run_secret_env("TELEGRAM_TOKEN", "backend")
anthropic_api_key_env = create_cloud_run_secret_env("ANTHROPIC_API_KEY", "backend")
mongodb_uri_env = create_cloud_run_secret_env("MONGODB_URI", "backend")
serpapi_api_key_env = create_cloud_run_secret_env("SERPAPI_API_KEY", "backend")

private_bucket_name_env = gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
    name="PRIVATE_BUCKET_NAME",
    value="nisse2050-private",
)

backend = create_cloud_run_with_monitoring(
    CloudRunServiceConfig(
        service_name="backend",
        image=docker_repository_url.apply(lambda url: f"{url}/backend:{repo_short_sha('..')}"),
        envs=[
            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="CLOUD", value="1"),
            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                name="WEBHOOK_URL",
                value="https://backend-675179615608.us-central1.run.app/webhook",
            ),
            telegram_token_env,
            anthropic_api_key_env,
            mongodb_uri_env,
            serpapi_api_key_env,
            private_bucket_name_env,
        ],
        resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
            cpu_idle=True,
            limits={"cpu": "1", "memory": "2Gi"},
        ),
        service_account_email=cloud_run_service_account.email,
        notification_channels=[],
        allow_unauthenticated=True,
        location=gcp.config.region,
        ingress="INGRESS_TRAFFIC_ALL",
        min_instances=0,
        max_instances=1,
    ),
)

backend_cloud_run_service = backend.service
