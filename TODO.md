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

## 3. CI (GitHub Actions) ✅ DONE

- [x] `.github/workflows/ci.yml` — 3 jobs `lint` / `typecheck` / `test`, each
      delegating to a `make` target (Makefile is the source of truth, no
      copy-paste). `make ci` runs the same three locally.
- [x] Triggers on `pull_request` and `push` to `main`
- [x] `test` job sets dummy `TELEGRAM_TOKEN` / `WEBHOOK_URL` so
      `test-backend-dry-run` boots (baski treats empty env as unset)
- [x] Guarded `include .env` in the Makefile so CI (no `.env`) doesn't hard-fail
- [x] `per-file-ignores` for `infrastructure/**` (Pulumi false positives) so lint is green
- [x] CI green on real PR #1 — lint / typecheck / test all pass
- [x] `infra` preview job: `.github/workflows/pulumi-preview.yml` (INFRA) added
      and **passing** on PR #1 (SERVICE_ACCOUNT secret wired, GCP auth works)

## 4. CD — deployment to `nisse2050` 🟡 IN PROGRESS

Infra has been `pulumi up`'d already (image `backend:5ab470e`), but the Cloud Run
service is **Ready=False** — two boot blockers below.

Done:

- [x] GCS state bucket `gs://nisse2050-pulumi` exists (Pulumi state under `.pulumi/`)
- [x] Pulumi stack `prod` + `make infra-apply` ran → Artifact Registry `docker`,
      `cloud-run@` SA, `TELEGRAM_TOKEN` secret container, Cloud Run `backend` service
- [x] IAM: `cloud-run@` + `deploy@` roles copied from clarity; `github-actions@`
      read-only SA created (CI preview uses it)
- [x] Cloud Build ↔ GitHub connection (`galilei2050-github-oauthtoken-*` secret)

Blockers (why `backend` is Ready=False):

- [ ] **`TELEGRAM_TOKEN` secret has NO version** — container exists, value never
      added. Run: `echo -n "<prod bot token>" | gcloud secrets versions add TELEGRAM_TOKEN --data-file=- --project nisse2050`
- [ ] **`WEBHOOK_URL` not set on the Cloud Run service** — `cloud_run_backend.py`
      omits it, so baski crashes on boot (`get_env("WEBHOOK_URL","")` → empty →
      raises). Set it to the service's own URL (Pulumi `service.uri`) and
      `pulumi up` again.

Remaining:

- [ ] `PULUMI_CONFIG_PASSPHRASE` in Secret Manager (cloudbuild.yaml reads it for
      `make cd`) — not present yet (GitHub Actions has its own secret)
- [ ] **Cloud Build trigger** on push to `main` → `cloudbuild.yaml` → `make cd`
      — none configured yet
- [ ] `WEBHOOK_URL` registered with Telegram so webhook mode receives updates
- [ ] **Live check**: message the deployed bot → it replies

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
