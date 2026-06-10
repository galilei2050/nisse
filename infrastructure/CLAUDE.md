# nisse infrastructure — Pulumi (GCP, project `nisse2050`)

Entry: `__main__.py` registers resources via side-effect imports (`build_triggers`, `delivery`,
`iam`, `services`). Add a resource = new top-level module or `services/<x>.py`, then import it so
it registers. Shared Cloud Run / Pulumi helpers come from `baski.infra` — don't reinvent.

## What Pulumi owns

| Module | Resource |
|---|---|
| `delivery.py` | Artifact Registry repo `docker` |
| `build_triggers.py` | Cloud Build trigger `backend-deploy` (push to main → `cloudbuild.yaml`) |
| `iam.py` | Runtime service account `cloud-run` |
| `services/cloud_run_backend.py` | Cloud Run service `backend` + 5xx alert (via `create_cloud_run_with_monitoring`) |
| `services/cloud_tasks.py` | Cloud Tasks queue `tg-update-queue` |

## What is provisioned BY HAND (never in Pulumi)

Identity, access, and storage are manual — Pulumi only describes compute. Do **not** add these
to the stack:

- **IAM bindings / role grants** — e.g. `run.invoker`, Secret Manager access, bucket access,
  Cloud Tasks enqueuer. Granted by hand in the console/`gcloud`.
- **Secrets + versions** (Secret Manager) and their grant to the `cloud-run` SA. Code only
  *references* them via `create_cloud_run_secret_env(name, "backend")`.
- **Buckets** — e.g. `nisse2050-private`. Referenced by name (env / code), not created here.
- **The `deploy` SA** (`deploy@nisse2050`) that Cloud Build runs as — manually managed.
- **GitHub 2nd-gen connection** `galilei2050` (Cloud Build) — created in the console.

## Cloud Tasks → worker auth

The webhook enqueues an OIDC-signed task that POSTs back to the `/tasks/update` worker. The token
is signed as the **`cloud-run@nisse2050` runtime SA** (reused — no dedicated invoker SA). The
backend is `allow_unauthenticated`, so no `run.invoker` grant is needed today; if ingress is ever
locked down, add that grant by hand.
