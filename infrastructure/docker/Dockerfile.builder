# CD builder image — bakes every tool AND the dependency cache `make cd` needs (docker,
# gcloud + docker-auth, pulumi, uv + warmed uv cache, make) so the deploy build
# (cloudbuild.yaml) downloads/installs nothing at runtime — it just runs `make cd`.
#
# Rebuilt by the `builder-image` Cloud Build trigger when this file, pyproject.toml or
# uv.lock change (infrastructure/build_triggers.py → cloudbuild.infra.yaml). Refreshed
# weekly via the builder-refresh GitHub Action (uv lock --upgrade → PR → merge → rebuild).
FROM gcr.io/google.com/cloudsdktool/cloud-sdk:slim

# docker CLI (daemon comes from Cloud Build), make, git. No --no-install-recommends:
# docker.io needs its recommends to land a working CLI on the cloud-sdk base.
RUN apt-get update && apt-get install -y docker.io git make \
    && rm -rf /var/lib/apt/lists/*

# Pulumi CLI → /root/.pulumi/bin
RUN curl -fsSL https://get.pulumi.com | sh
ENV PATH="/root/.pulumi/bin:$PATH"

# uv → /usr/local/bin (already on PATH, so make and its `uv run` children find it)
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Warm the uv cache from the locked deps. Cloud Build overwrites /workspace with the repo
# checkout, so a baked .venv there wouldn't survive — but ~/.cache/uv does, so the deploy's
# `uv sync` materializes .venv from cache with zero downloads (~seconds, hardlinks only).
COPY pyproject.toml uv.lock README.md /opt/deps-cache/
RUN cd /opt/deps-cache && uv sync --frozen --no-install-project

# Bake docker → Artifact Registry auth (writes /root/.docker/config.json, survives the
# /workspace mount) so the deploy never needs `gcloud auth configure-docker`.
RUN gcloud auth configure-docker us-docker.pkg.dev --quiet

WORKDIR /workspace
