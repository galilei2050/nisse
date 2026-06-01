"""Cloud Build triggers fired on push to main.

- backend-deploy: cloudbuild.yaml → `make cd` (build + deploy the backend).
- builder-image:  cloudbuild.infra.yaml → `make builder-image-push` (re-bake the CD builder
  image when its inputs change). Separate trigger, like clarity's base-images build.
"""

import pulumi_gcp as gcp
from pulumi_gcp import cloudbuild

_PROJECT = gcp.config.project
_REGION = gcp.config.region
# GitHub repo wired to Cloud Build via the `galilei2050` 2nd-gen connection (created out of
# band in the console). Builds run as the manually-managed `deploy` SA.
_REPO = f"projects/{_PROJECT}/locations/{_REGION}/connections/galilei2050/repositories/galilei2050-nisse"
_SA = f"projects/{_PROJECT}/serviceAccounts/deploy@{_PROJECT}.iam.gserviceaccount.com"

# Inputs that change the builder image — owned by the builder-image trigger. The deploy
# trigger ignores them so a pure dep/builder change only re-bakes the image, not a deploy.
_BUILDER_INPUTS = ["infrastructure/docker/**", "pyproject.toml", "uv.lock"]

backend_deploy_trigger = cloudbuild.Trigger(
    "backend-deploy",
    name="backend-deploy",
    location=_REGION,
    description="Deploy nisse backend on push to main",
    repository_event_config={"push": {"branch": "^main$"}, "repository": _REPO},
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
    # Dep/builder-only changes re-bake the image (builder-image trigger), not deploy.
    ignored_files=_BUILDER_INPUTS,
    filename="cloudbuild.yaml",
    service_account=_SA,
    include_build_logs="INCLUDE_BUILD_LOGS_WITH_STATUS",
)

builder_image_trigger = cloudbuild.Trigger(
    "builder-image",
    name="builder-image",
    location=_REGION,
    description="Re-bake the CD builder image when its tools/deps change",
    repository_event_config={"push": {"branch": "^main$"}, "repository": _REPO},
    included_files=_BUILDER_INPUTS,
    filename="cloudbuild.infra.yaml",
    service_account=_SA,
    include_build_logs="INCLUDE_BUILD_LOGS_WITH_STATUS",
)
