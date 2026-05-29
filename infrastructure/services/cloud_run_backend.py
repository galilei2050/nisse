"""Cloud Run service for the nisse Telegram backend."""

import pulumi_gcp as gcp
from baski.infra.run import (
    CloudRunServiceConfig,
    create_cloud_run_with_monitoring,
    repo_short_sha,
)

from delivery import docker_repository_url
from iam import cloud_run_service_account
from secret_manager import telegram_token

telegram_token_env = gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
    name="TELEGRAM_TOKEN",
    value_source=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceArgs(
        secret_key_ref=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceSecretKeyRefArgs(
            secret=telegram_token.secret_id,
            version="latest",
        ),
    ),
)

backend = create_cloud_run_with_monitoring(
    CloudRunServiceConfig(
        service_name="backend",
        image=docker_repository_url.apply(lambda url: f"{url}/backend:{repo_short_sha('..')}"),
        envs=[
            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="CLOUD", value="1"),
            telegram_token_env,
        ],
        resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
            cpu_idle=True,
            limits={"cpu": "1", "memory": "512Mi"},
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
