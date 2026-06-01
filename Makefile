# Load .env when present (local dev). Absent in CI — guard so make doesn't hard-fail.
ifneq (,$(wildcard ./.env))
    include .env
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

.PHONY: lint
lint:
	uv run ruff format --check app/ infrastructure/
	uv run ruff check app/ infrastructure/
	uv run python3 anon_lint.py --recursive app/ infrastructure/

.PHONY: lint-fix
lint-fix:
	uv run ruff format app/ infrastructure/
	uv run ruff check app/ infrastructure/ --fix
	uv run python3 anon_lint.py --recursive app/ infrastructure/

.PHONY: typecheck
typecheck:
	uv run mypy app/

# Functional tests — pure, no running backend. Part of `make test` / CI.
.PHONY: test-backend
test-backend:
	uv run pytest tests/backend/

# Smoke — boot the real bot (polling) and verify it's healthy against Telegram.
# Mirrors clarity: backend-run → backend-wait → pytest tests/smoke. Leaves the bot
# running. Needs a real TELEGRAM_TOKEN; not in GitHub `ci` (no token there).
.PHONY: smoke-test
smoke-test: backend-run backend-wait
	uv run pytest tests/smoke/

# backend-wait — poll until healthy. Polling has no HTTP; the "Run polling for
# bot @…" log line (aiogram getMe succeeded) is the ready signal.
.PHONY: backend-wait
backend-wait:
	@for i in $$(seq 1 20); do \
		sleep 1; \
		if ! pgrep -f '[a]pp.backend' >/dev/null; then echo "Backend died — see tmp/backend.log"; exit 1; fi; \
		if grep -q 'Run polling for bot @' ~/Logs/nisse-backend.log 2>/dev/null; then echo "Backend ready (polling)!"; exit 0; fi; \
	done; echo "Backend failed to start in 20s — see tmp/backend.log"; exit 1

# Single source of truth for CI — GitHub Actions just runs `make ci`, no copy-paste.
# Smoke excluded: needs a live backend with a real token + public WEBHOOK_URL.
.PHONY: ci
ci: lint typecheck test-backend test-backend-dry-run

.PHONY: test
test: ci

# Git hook entry points (.pre-commit-config.yaml): fast auto-fix on commit,
# full ci + a real-bot smoke boot on push (mirrors clarity's pre-push-check).
.PHONY: pre-commit
pre-commit: lint-fix

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
