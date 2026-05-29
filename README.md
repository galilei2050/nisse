# nisse

Personal AI assistant Telegram bot. Backend deploys to Cloud Run on GCP project `nisse2050`.

Built on [baski](https://github.com/galilei2050/baski) — shared foundation library (FastAPI/Telegram server template, GCP integrations).

## Layout

```
app/                FastAPI / TelegramServer backend
infrastructure/     Pulumi IaC: Cloud Run + service accounts
Dockerfile          Container image (python:3.12-slim base)
cloudbuild.yaml     Cloud Build pipeline → builds image, applies Pulumi
Makefile            Local + CD targets
config.yml          Runtime YAML config (overridden by Firestore in cloud)
```

## Local

```bash
make setup                          # uv sync + pre-commit install
export TELEGRAM_TOKEN=<bot token>
make backend-run                    # polling mode, no public URL needed
```

## Deploy

Pulumi owns Artifact Registry, Cloud Run, the `cloud-run` service account, the
`TELEGRAM_TOKEN` secret container, and its IAM binding. Two things must exist
*before* the first `pulumi up` because Pulumi itself depends on them:

1. GCS bucket for Pulumi state: `gsutil mb -p nisse2050 -l us-central1 gs://nisse2050-pulumi`
2. `PULUMI_CONFIG_PASSPHRASE` secret in Secret Manager (any random string).

Then:

```bash
make infra-setup       # pulumi login + stack init/select
make infra-apply       # creates registry, SA, secret container, IAM
# now set the actual token value:
echo -n "<bot token>" | gcloud secrets versions add TELEGRAM_TOKEN --data-file=-
make cd                # build + push image, pulumi up
```
