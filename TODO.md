# Scaffold — Definition of Done

The scaffold is **not** finished until the bot is deployed. "Code exists" ≠ done.
Done means: runs locally, CI is green on PR, CD deploys on merge, and the live
Cloud Run service answers. Below is the checklist to get there.

Status legend: `[ ]` todo · `[~]` partial · `[x]` done

---

## 1. Run locally ✅

- [x] `make setup` succeeds on a clean checkout (`uv sync` + pre-commit install)
- [x] `make backend-run` boots in polling mode and `@nisse_chat_bot` replies
      `hello` in Telegram (verified — "Start polling / Run polling for bot
      @nisse_chat_bot")
- [x] `app/backend.py` entry point + `app/hello/` router wired on the dispatcher
- [x] `make test-backend-dry-run` (`python -m app.backend --dry-run`) exits 0 —
      import/boot smoke check CI will reuse

> Gotcha: baski treats an **empty** env value as "not set" (`baski/env.py`
> EnvValue raises on `""`). `TelegramServer.add_arguments` calls
> `get_env("WEBHOOK_URL", "")`, so a missing/empty `WEBHOOK_URL` crashes boot
> even in polling mode. Keep `WEBHOOK_URL` set in `.env`.
> Note: `.env` is loaded only via the Makefile (`include .env; export`) — a bare
> `python -m app.backend` won't see it. Add `load_dotenv()` (like clarity's
> `app/cli.py`) if direct runs are needed.

## 2. Tests — functional + smoke split ✅

Two suites, mirroring clarity (`tests/backend/` + `tests/smoke/`); tests never
provision env — the runner does (CI job / local `.env`).

- [x] `tests/backend/` — functional, pure, no running backend:
      `test_backend.py` (router + middleware wired), `test_access.py`
      (allow-list: owner passes, stranger turned away), `test_hello.py`
      (handler replies "hello"). `conftest.py` builds `NisseBot` via the `bot`
      fixture (clean argv). Run with `make test-backend` — 5 passed.
- [x] `tests/smoke/` — against a REAL backend at `BACKEND_URL`: GET `/ping` and
      `/` return `"OK"` (baski webhook server). `make smoke-test`. Not in
      `make test` — needs a live service (post-deploy / local `--cloud`).
- [x] `make test` = `lint typecheck test-backend test-backend-dry-run`; CI runs
      `make test-backend`.

## 3. CI (GitHub Actions) ✅

- [x] Added `.github/workflows/ci.yml` with jobs `lint`, `typecheck`, `test`
      (uv + setup-python 3.12 + `make setup` then the granular target)
- [x] Triggers on `pull_request` and `push` to `main`
- [x] `test` job sets dummy `TELEGRAM_TOKEN` / `WEBHOOK_URL` so
      `test-backend-dry-run` boots (baski treats empty env as unset)
- [x] Guarded `include .env` in the Makefile so CI (no `.env`) doesn't hard-fail
- [x] Added `per-file-ignores` for `infrastructure/**` (Pulumi false positives:
      INP001/S106/D104) so `make lint` is green
- [ ] Confirm CI passes green on a real PR (push branch + open PR)
- [ ] Optional: add `infra` preview job (mirrors clarity `pulumi-preview.yml`):
      `pulumi preview` on PRs touching `infrastructure/**` (needs GCP auth secret)

## 4. CD — successful deployment to `nisse2050`

One-time prerequisites (must exist BEFORE first `pulumi up`; Pulumi depends on them):

- [ ] GCS state bucket: `gsutil mb -p nisse2050 -l us-central1 gs://nisse2050-pulumi`
- [ ] `PULUMI_CONFIG_PASSPHRASE` secret in Secret Manager (any random string)
- [ ] Cloud Build service account has roles: Artifact Registry writer,
      Cloud Run admin, Secret Manager admin, Service Account user, Storage admin

Pipeline:

- [ ] `make infra-setup` — `pulumi login` + stack `prod` init/select + gcp config
- [ ] `make infra-apply` (or `infra-preview` first) creates Artifact Registry,
      `cloud-run` SA, `TELEGRAM_TOKEN` secret container, IAM binding, Cloud Run svc
- [ ] Set the real token value:
      `echo -n "<bot token>" | gcloud secrets versions add TELEGRAM_TOKEN --data-file=-`
- [ ] Connect a **Cloud Build trigger** to the GitHub repo (push to `main` →
      runs `cloudbuild.yaml` → `make cd`). Today `cloudbuild.yaml` exists but
      nothing invokes it — wire the trigger (GCP console or
      `gcloud builds triggers create github`)
- [ ] First `make cd` (or triggered build) builds + pushes the image and runs
      `pulumi up` end-to-end without manual steps
- [ ] `WEBHOOK_URL` is set to the deployed Cloud Run URL and registered with
      Telegram so webhook mode (`--cloud`) actually receives updates
- [ ] **Live check**: message the deployed bot on Telegram → it replies `hello`

## 5. Docs / housekeeping

- [x] README documents local + deploy flow
- [ ] Update README/CLAUDE.md once CI + Cloud Build trigger exist (commands changed)
- [ ] `.env` created locally from `.env.example` with a real token (gitignored)

---

## Exit criteria (all must hold)

1. Fresh clone → `make setup && make backend-run` → bot replies locally.
2. `make test` runs pytest (incl. the smoke test) and is green.
3. PR → CI green (lint + typecheck + test).
4. Merge to `main` → Cloud Build trigger fires → image built → `pulumi up` →
   Cloud Run live.
5. The deployed bot answers a real Telegram message.
