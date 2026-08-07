# nisse infrastructure — Pulumi (GCP, project `nisse2050`)

## What belongs here

This stack provisions **compute and message plumbing** — the things that run code and move data
between services: Cloud Run services, Cloud Tasks queues, container delivery, build triggers.

It deliberately owns **everything that is neither security nor storage**:

- **Security / identity is out** — IAM role grants, Secret Manager secrets, and service-account
  permissions are provisioned **by hand**. A human owns the security boundary; Pulumi never grants
  access or holds a secret. Code only *references* secrets (`create_cloud_run_secret_env`) and
  reuses existing SAs by name.
- **Storage / state is out** — buckets are created by hand; the database (**MongoDB, external —
  Atlas**) is not a GCP resource at all, reached only via the `MONGODB_URI` secret. **No bucket or
  DB resource lives in this stack.**

Rule of thumb before adding a resource: if it grants access, holds a secret, or persists data —
it's manual, not here.

## Entry & layout

`__main__.py` registers resources via side-effect imports (`build_triggers`, `delivery`, `iam`,
`services`). Add a resource = new top-level module or `services/<x>.py`, then import it so it
registers. Shared Cloud Run / Pulumi helpers come from `baski.infra` — don't reinvent.

| Module | Resource |
|---|---|
| `delivery.py` | Artifact Registry repo `docker` |
| `build_triggers.py` | Cloud Build trigger `backend-deploy` (push to main → `cloudbuild.yaml`) |
| `iam.py` | Runtime service account `cloud-run` (the identity; its *grants* are manual) |
| `services/cloud_run_backend.py` | Cloud Run service `backend` + 5xx alert (via `create_cloud_run_with_monitoring`) |
| `services/cloud_tasks.py` | Cloud Tasks queue `tg-update-queue` |
| `services/curator_schedule.py` | Cloud Scheduler job `curator-nightly` (04:00 PT → `POST /curate`) |
| `services/schedule_sweep.py` | Cloud Scheduler job `schedule-sweep` (every 15 min → `POST /schedule/sweep`) — the net under the at-most-once task queue |

## Provisioned by hand (never in Pulumi)

IAM grants (`run.invoker`, Secret Manager / bucket access) · Secret Manager secrets + versions ·
buckets (e.g. `nisse2050-private`) · the MongoDB cluster · the `deploy@nisse2050` SA Cloud Build
runs as · the GitHub 2nd-gen connection `galilei2050`.

## Cloud Tasks → worker auth

The webhook enqueues an OIDC-signed task that POSTs back to the `/tasks/update` worker, signed as
the **`cloud-run@nisse2050` runtime SA** (reused — no dedicated invoker SA). The backend is
`allow_unauthenticated`, so no `run.invoker` grant is needed today; if ingress is ever locked down,
add that grant by hand.
