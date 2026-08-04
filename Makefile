# git exports GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE to its hooks, and they OVERRIDE both `-C` and
# repo discovery — so a `git` call from inside pre-commit reads whatever repo the hook fired for.
GIT_CLEAN_ENV := env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE

# The main checkout, identical from every git worktree (`.claude/worktrees/<x>/`): the git dir is
# shared, so anchoring on it is what makes `make` work from a worktree at all. Empty outside a repo.
NISSE_ROOT := $(shell $(GIT_CLEAN_ENV) git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)/..

# Load .env when present (local dev). It's git-ignored, so it exists ONLY in the main checkout —
# read it from there, or every target run from a worktree loses the secrets. Absent in CI: guarded.
ifneq (,$(wildcard $(NISSE_ROOT)/.env))
    include $(NISSE_ROOT)/.env
    export
endif

SHELL := /bin/bash
SHORT_COMMIT_SHA := $(shell git rev-parse --short HEAD)
BUILD_DATE := $(shell date +%Y%m%d)
export GOOGLE_CLOUD_PROJECT := nisse2050
export GOOGLE_CLOUD_REGION := us-central1
export PULUMI_STATE_BUCKET := nisse2050-pulumi
export DOCKER_REPOSITORY_ROOT := us-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/docker
export BACKEND_IMAGE := ${DOCKER_REPOSITORY_ROOT}/backend:${SHORT_COMMIT_SHA}
export BACKEND_IMAGE_LATEST := ${DOCKER_REPOSITORY_ROOT}/backend:latest
export BUILDER_IMAGE := ${DOCKER_REPOSITORY_ROOT}/builder:latest
export BUILDER_IMAGE_DATED := ${DOCKER_REPOSITORY_ROOT}/builder:${BUILD_DATE}
export PATH := $(HOME)/.pulumi/bin:$(PATH)   # $(HOME) — make doesn't expand ~ inside PATH

# Pulumi runs from infrastructure/ but shares the root .venv.
PULUMI := cd infrastructure && uv run pulumi

.PHONY: setup
setup:
	# Tools + deps so the box can build, test, and deploy. (Backend targeting — login,
	# stack, config — lives in `infra-setup`, not here: that's not tooling.)
	curl -fsSL https://get.pulumi.com | sh    # Pulumi CLI → ~/.pulumi/bin (on PATH, export above)
	uv sync
	$(PULUMI) install                         # Pulumi resource plugins (gcp) — dep prep, no stack needed
	uv run pre-commit install --hook-type pre-commit --hook-type pre-push
	gcloud auth configure-docker us-docker.pkg.dev --quiet   # docker push → Artifact Registry

# Local dev / smoke — polling. Single instance: one bot token can't have two
# pollers, so stop any running one first. Token from env TELEGRAM_TOKEN.
.PHONY: backend-run
backend-run:
	@pkill -f '[a]pp.backend' 2>/dev/null; true   # bracket avoids pkill matching its own recipe shell
	@mkdir -p ~/Logs tmp
	nohup uv run python -m app.backend > ~/Logs/nisse-backend.log 2>&1 < /dev/null & disown
	@ln -sf ~/Logs/nisse-backend.log tmp/backend.log
	@echo "Backend (polling) started — logs: tmp/backend.log"

.PHONY: test-backend-dry-run
test-backend-dry-run:
	uv run python -m app.backend --dry-run

# Manual end-to-end probe — one Assistant.reply(); prints injected context, tool calls, answer.
# Real API/DB calls; throwaway user. `make probe MSG="…" [U=42]`. See docs/memory-test-cases.md.
.PHONY: probe
probe:
	uv run python -m app.probe --user-id $(or $(U),1) --message "$(MSG)"

# One curator maintenance pass, outside Cloud Scheduler: prints evidence, changes, and the report.
# `make curate U=<conversation_id> [DAYS=7] [DRY=1]`. Real API/DB — it edits the live stores unless DRY=1.
.PHONY: curate
curate:
	uv run python -m app.curate_probe --conversation-id $(U) --days $(or $(DAYS),1) $(if $(DRY),--dry-run,)

# Companion to curate: dump one conversation's change history (who changed what, and what it replaced).
# `make revisions U=<conversation_id> [RUN=<run_id>]`.
.PHONY: revisions
revisions:
	uv run python scripts/show_revisions.py $(U) $(RUN)

# Seed a conversation's sub-agents from app/subagents/agents.yml. `make seed U=<conversation_id>` (or U=all).
.PHONY: seed
seed:
	uv run python -m scripts.seed_subagents $(U)

# Companion to probe: dump the long-term `memories` collection (live + soft-deleted).
# `make memories U=<conversation_id>` for one chat in full; `make memories` groups ALL chats by id.
.PHONY: memories
memories:
	uv run python scripts/show_memories.py $(U)

# Companion to probe: dump one conversation's `conversation_turns` (active + soft-deleted).
# `make turns U=<conversation_id>`. See docs/history-test-cases.md.
.PHONY: turns
turns:
	uv run python scripts/show_turns.py $(U)

.PHONY: lint
lint:
	uv run ruff format --check app/ infrastructure/
	uv run ruff check app/ infrastructure/
	uv run python -m baski_lint --recursive app/ infrastructure/

.PHONY: lint-fix
lint-fix:
	uv run ruff format app/ infrastructure/
	uv run ruff check app/ infrastructure/ --fix
	uv run python -m baski_lint --recursive app/ infrastructure/

.PHONY: typecheck
typecheck:
	uv run mypy app/

# Functional tests — pure, no running backend. Part of `make test` / CI.
.PHONY: test-backend
test-backend:
	# Everything except tests/smoke/, which needs the built image running (see test-backend-image).
	# Discovered, not listed: an enumerated list silently skips a newly added test directory.
	uv run pytest tests/ --ignore=tests/smoke

# Smoke — boot the real bot (polling) and verify it's healthy against Telegram. Leaves the
# bot running. Needs a real TELEGRAM_TOKEN — the LOCAL bot, separate from prod's (see
# CLAUDE.md "Local vs prod"), so this never disturbs the prod webhook. Not in GitHub `ci`.
.PHONY: smoke-test
smoke-test: backend-run backend-local-wait
	uv run pytest tests/smoke/

# Readiness for the polling bot: polling opens no HTTP port, so the ready signal is the
# "Run polling for bot @…" log line (aiogram getMe succeeded), not a port.
.PHONY: backend-local-wait
backend-local-wait:
	@for i in $$(seq 1 20); do \
		sleep 1; \
		if ! pgrep -f '[a]pp.backend' >/dev/null; then echo "Backend died — see tmp/backend.log"; exit 1; fi; \
		if grep -q 'Run polling for bot @' ~/Logs/nisse-backend.log 2>/dev/null; then echo "Backend ready (polling)!"; exit 0; fi; \
	done; echo "Backend failed to start in 20s — see tmp/backend.log"; exit 1

# Boot the real deploy image and run the smoke suite against it. Catches startup crashes
# that --dry-run can't: dry-run returns before the lifespan runs, so it never launches the
# browser or opens a client. Not in `ci` (needs Docker + live secrets) — its own GitHub job.
.PHONY: test-backend-image
test-backend-image: backend-image-run backend-cloud-wait
	BACKEND_URL=http://localhost:8080 uv run pytest tests/smoke/

# Start the deploy image; its entrypoint runs --cloud, so it serves the webhook HTTP port and
# builds a Cloud Tasks client whose constructor needs Application Default Credentials. Env comes
# from the caller's environment (CI job `env:` / local .env) — never baked in; when GCP creds are
# present (CI mints them via WIF) they're mounted into the container.
.PHONY: backend-image-run
backend-image-run: backend-docker-build
	@docker rm -f nisse-smoke 2>/dev/null || true
	@set -a; [ -f .env ] && . ./.env || true; set +a; \
		gcp=""; \
		if [ -n "$$GOOGLE_APPLICATION_CREDENTIALS" ]; then \
			gcp="-v $$GOOGLE_APPLICATION_CREDENTIALS:$$GOOGLE_APPLICATION_CREDENTIALS:ro -e GOOGLE_APPLICATION_CREDENTIALS"; \
		fi; \
		docker run -d --name nisse-smoke -p 8080:8080 -e PORT=8080 $$gcp \
			-e TELEGRAM_TOKEN -e WEBHOOK_URL -e MONGODB_URI -e ANTHROPIC_API_KEY -e ELEVENLABS_API_KEY \
			-e GOOGLE_CLOUD_PROJECT -e GOOGLE_CLOUD_REGION -e CLOUD_TASKS_QUEUE -e PRIVATE_BUCKET_NAME \
			${BACKEND_IMAGE_LATEST}
	@echo "Backend image started — logs: docker logs nisse-smoke"

# Readiness for the HTTP backend (webhook mode serves a port): curl /ping until it answers;
# fail fast and dump container logs if it dies first.
.PHONY: backend-cloud-wait
backend-cloud-wait:
	@for i in $$(seq 1 30); do \
		if [ "$$(docker inspect -f '{{.State.Running}}' nisse-smoke 2>/dev/null)" != "true" ]; then echo "Container exited:"; docker logs nisse-smoke; exit 1; fi; \
		if curl -fsS http://localhost:8080/ping >/dev/null 2>&1; then echo "Backend image ready — /ping OK"; exit 0; fi; \
		sleep 2; \
	done; \
	echo "Backend image failed to start in 60s:"; docker logs nisse-smoke; exit 1

# Single source of truth for CI — GitHub Actions just runs `make ci`, no copy-paste.
# Smoke excluded: needs Docker + live secrets, so it runs as its own GitHub job.
.PHONY: ci
ci: lint typecheck test-backend test-backend-dry-run

.PHONY: test
test: ci

# Git hook entry points (.pre-commit-config.yaml): fast auto-fix on commit,
# full ci + a real-bot smoke boot on push.
# baski is nisse's sibling. Resolve it through NISSE_ROOT, not a CWD-relative `../baski`, which from
# a worktree points at `.claude/worktrees/baski` — a directory that doesn't exist.
BASKI_DIR ?= $(NISSE_ROOT)/../baski
BASKI_GIT := $(GIT_CLEAN_ENV) git -C $(BASKI_DIR)

.PHONY: check-baski
check-baski:
	@$(BASKI_GIT) fetch -q origin main
	@test "$$($(BASKI_GIT) rev-parse --abbrev-ref HEAD)" = main || { echo "ERROR: baski is not on main"; exit 1; }
	@$(BASKI_GIT) diff --quiet HEAD || { echo "ERROR: baski has uncommitted changes"; exit 1; }
	@test "$$($(BASKI_GIT) rev-parse HEAD)" = "$$($(BASKI_GIT) rev-parse FETCH_HEAD)" || { echo "ERROR: baski main differs from origin/main — pull/push baski"; exit 1; }

.PHONY: pre-commit
pre-commit: check-baski lint-fix
	uv sync

.PHONY: pre-push
pre-push: ci smoke-test

# Docker
# Builder image — the CD toolchain (docker, gcloud, pulumi, uv, make) baked once. Run
# `make builder-image-push` when infrastructure/docker/Dockerfile.builder changes; the
# Cloud Build trigger then deploys from builder:latest (see cloudbuild.yaml).
.PHONY: builder-image-build
builder-image-build:
	@echo "Building builder image ${BUILDER_IMAGE}"
	docker build -t ${BUILDER_IMAGE} -t ${BUILDER_IMAGE_DATED} -f infrastructure/docker/Dockerfile.builder .

.PHONY: builder-image-push
builder-image-push: builder-image-build
	docker push ${BUILDER_IMAGE}
	docker push ${BUILDER_IMAGE_DATED}

.PHONY: backend-docker-build
backend-docker-build:
	@echo "Building image ${BACKEND_IMAGE}"
	docker build -t ${BACKEND_IMAGE} -t ${BACKEND_IMAGE_LATEST} --cache-from ${BACKEND_IMAGE_LATEST} .

.PHONY: backend-docker-push
backend-docker-push: backend-docker-build
	docker push ${BACKEND_IMAGE}
	docker push ${BACKEND_IMAGE_LATEST}

# Pulumi
# Backend targeting: point Pulumi at the state bucket + select/create the stack + set
# config. Has a network login, so it's NOT a prereq of the infra ops below — run it once
# per session, then iterate fast with infra-diff/refresh. `cd` calls it explicitly.
# (Tooling install lives in `setup`.)
.PHONY: infra-setup
infra-setup:
	$(PULUMI) login gs://${PULUMI_STATE_BUCKET}
	# select-or-init in one shell (no second cd — $(PULUMI) already chdirs to infrastructure).
	cd infrastructure && (uv run pulumi stack select prod || uv run pulumi stack init prod --secrets-provider=passphrase)
	$(PULUMI) config set gcp:project ${GOOGLE_CLOUD_PROJECT}
	$(PULUMI) config set gcp:region ${GOOGLE_CLOUD_REGION}

.PHONY: infra-preview
infra-preview:
	$(PULUMI) preview

# Preview + capture to infrastructure/preview.txt for the PR comment (INFRA workflow).
.PHONY: infra-diff
infra-diff:
	$(PULUMI) preview --diff --non-interactive 2>&1 | tee preview.txt

.PHONY: infra-apply
infra-apply:
	$(PULUMI) up --yes --skip-preview

.PHONY: infra-deploy
infra-deploy: infra-setup backend-docker-push
	$(MAKE) infra-apply

# Full deploy: bootstrap backend (infra-setup), build+push image, then pulumi up.
# infra-setup first so a bad login/stack fails fast, before the docker build.
# Assumes `make setup` already ran on this box/container.
.PHONY: cd
cd: infra-setup backend-docker-push infra-apply
