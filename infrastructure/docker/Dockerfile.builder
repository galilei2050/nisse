# CD builder image — bakes every tool `make cd` needs (docker, gcloud, pulumi, uv, make)
# so cloudbuild.yaml just runs `make cd`, with no flaky runtime apt-get/curl installs.
# Rebuild + push when this file changes:  make builder-image-push
FROM gcr.io/google.com/cloudsdktool/cloud-sdk:slim

# docker CLI (daemon is provided by Cloud Build), make, git. No --no-install-recommends:
# docker.io needs its recommends to land a working CLI on the cloud-sdk base.
RUN apt-get update && apt-get install -y docker.io git make \
    && rm -rf /var/lib/apt/lists/*

# Pulumi CLI → /root/.pulumi/bin
RUN curl -fsSL https://get.pulumi.com | sh
ENV PATH="/root/.pulumi/bin:$PATH"

# uv → /usr/local/bin (already on PATH, so make and its `uv run` children find it)
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

WORKDIR /workspace
