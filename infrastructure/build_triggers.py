"""Cloud Build trigger: push to main → cloudbuild.yaml → make cd."""

import pulumi_gcp as gcp
from pulumi_gcp import cloudbuild

_PROJECT = gcp.config.project
_REGION = gcp.config.region

# GitHub repo wired to Cloud Build via the `galilei2050` 2nd-gen connection (created out
# of band in the console). Builds run as the manually-managed `deploy` SA — set here, not
# in cloudbuild.yaml, because the runtime SA is a trigger property.
backend_deploy_trigger = cloudbuild.Trigger(
    "backend-deploy",
    name="backend-deploy",
    location=_REGION,
    description="Deploy nisse backend on push to main",
    repository_event_config={
        "push": {"branch": "^main$"},
        "repository": (
            f"projects/{_PROJECT}/locations/{_REGION}/connections/galilei2050/repositories/galilei2050-nisse"
        ),
    },
    # Only build when something that can change the deploy artifact moves. Commits touching
    # only docs/.claude/etc. skip the trigger entirely (no build minutes, no pulumi noise).
    included_files=[
        "app/**",
        "infrastructure/**",
        "Dockerfile",
        ".dockerignore",
        "pyproject.toml",
        "uv.lock",
        "Makefile",
        "cloudbuild.yaml",
        "entrypoint.sh",
        "config.yml",
    ],
    filename="cloudbuild.yaml",
    service_account=f"projects/{_PROJECT}/serviceAccounts/deploy@{_PROJECT}.iam.gserviceaccount.com",
    include_build_logs="INCLUDE_BUILD_LOGS_WITH_STATUS",
)
