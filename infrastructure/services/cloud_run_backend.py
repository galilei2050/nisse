"""Cloud Run service for the nisse Telegram backend — uses baski.infra helpers."""

import pulumi_gcp as gcp
from baski.infra.iam import get_service_account
from baski.infra.run import (
    CloudRunServiceConfig,
    create_cloud_run_secret_env,
    create_cloud_run_with_monitoring,
    repo_short_sha,
)

DOCKER_REPOSITORY = f"us-docker.pkg.dev/{gcp.config.project}/docker"

cloud_run_service_account = get_service_account("cloud-run")

backend = create_cloud_run_with_monitoring(
    CloudRunServiceConfig(
        service_name="backend",
        image=f"{DOCKER_REPOSITORY}/backend:{repo_short_sha('..')}",
        envs=[
            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="CLOUD", value="1"),
            create_cloud_run_secret_env("TELEGRAM_TOKEN", "backend"),
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
