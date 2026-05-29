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
make setup                                  # one-shot venv + pip install
export TELEGRAM_TOKEN=<your bot token>
python3 -m app.backend                      # polling mode
```

Polling mode talks to Telegram directly — no public URL needed.

## Deploy

Prerequisites (one-time, set up by hand in `nisse2050`):

1. Artifact Registry repo named `docker` in `us-central1` (`gcloud artifacts repositories create docker --repository-format=docker --location=us-central1`)
2. Secret Manager secrets: `TELEGRAM_TOKEN`, `PULUMI_CONFIG_PASSPHRASE`
3. GCS bucket for Pulumi state: `gs://nisse2050-pulumi`
4. Service accounts: `cloud-run@nisse2050.iam.gserviceaccount.com` (with access to `TELEGRAM_TOKEN`)

Then `make cd` (or push to `main` → Cloud Build trigger).
