# Load .env when present (local dev). Absent in CI — guard so make doesn't hard-fail.
ifneq (,$(wildcard ./.env))
    include .env
    export
endif

SHELL := /bin/bash
SHORT_COMMIT_SHA := $(shell git rev-parse --short HEAD)
export GOOGLE_CLOUD_PROJECT := nisse2050
export GOOGLE_CLOUD_REGION := us-central1
export PULUMI_STATE_BUCKET := nisse2050-pulumi
export DOCKER_REPOSITORY_ROOT := us-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/docker
export BACKEND_IMAGE := ${DOCKER_REPOSITORY_ROOT}/backend:${SHORT_COMMIT_SHA}
export BACKEND_IMAGE_LATEST := ${DOCKER_REPOSITORY_ROOT}/backend:latest
export PATH := ~/.pulumi/bin:$(PATH)

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

# Local dev — polling. Same command everywhere; the token comes from the
# environment's TELEGRAM_TOKEN (.env locally, Secret Manager in Cloud Run).
.PHONY: backend-run
backend-run:
	@fuser -k 8080/tcp 2>/dev/null; true
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

# Smoke — boot the same app in webhook mode on :8080 (TELEGRAM_TOKEN from the
# environment), wait for /ping, run smoke tests, then stop it. Not part of `ci`;
# needs TELEGRAM_TOKEN + public WEBHOOK_URL.
.PHONY: smoke-test
smoke-test: backend-cloud-run backend-wait
	uv run pytest tests/smoke/; rc=$$?; fuser -k 8080/tcp 2>/dev/null; exit $$rc

.PHONY: backend-cloud-run
backend-cloud-run:
	@fuser -k 8080/tcp 2>/dev/null; true
	@mkdir -p ~/Logs tmp
	nohup uv run python -m app.backend --cloud -p 8080 > ~/Logs/nisse-smoke.log 2>&1 < /dev/null & disown
	@ln -sf ~/Logs/nisse-smoke.log tmp/smoke.log
	@echo "Smoke backend (webhook) booting on :8080"

.PHONY: backend-wait
backend-wait:
	@for i in $$(seq 1 20); do \
		sleep 1; \
		if ! pgrep -f 'app.backend --cloud' >/dev/null; then echo "Smoke backend died — see tmp/smoke.log"; exit 1; fi; \
		if curl -sf http://localhost:8080/ping >/dev/null 2>&1; then echo "Smoke backend ready!"; exit 0; fi; \
	done; echo "Smoke backend failed to start in 20s — see tmp/smoke.log"; exit 1

# Single source of truth for CI — GitHub Actions just runs `make ci`, no copy-paste.
# Smoke excluded: needs a live backend with a real token + public WEBHOOK_URL.
.PHONY: ci
ci: lint typecheck test-backend test-backend-dry-run

.PHONY: test
test: ci

# Pre-push git hook (.pre-commit-config.yaml) runs this. Smoke is excluded —
# it needs a live backend, which a local push can't assume.
.PHONY: pre-push
pre-push: test

# Docker
.PHONY: backend-docker-build
backend-docker-build:
	@echo "Building image ${BACKEND_IMAGE}"
	docker build -t ${BACKEND_IMAGE} -t ${BACKEND_IMAGE_LATEST} --cache-from ${BACKEND_IMAGE_LATEST} .

.PHONY: backend-docker-push
backend-docker-push: backend-docker-build
	docker push ${BACKEND_IMAGE}
	docker push ${BACKEND_IMAGE_LATEST}

# Pulumi
# Backend targeting only: point Pulumi at the state bucket + select/create the stack +
# set stack config. Cheap and idempotent, so every infra op below depends on it — you
# never have to remember to run it first. (Tooling install lives in `setup`.)
.PHONY: infra-setup
infra-setup:
	$(PULUMI) login gs://${PULUMI_STATE_BUCKET}
	# select-or-init in one shell (no second cd — $(PULUMI) already chdirs to infrastructure).
	cd infrastructure && (uv run pulumi stack select prod || uv run pulumi stack init prod --secrets-provider=passphrase)
	$(PULUMI) config set gcp:project ${GOOGLE_CLOUD_PROJECT}
	$(PULUMI) config set gcp:region ${GOOGLE_CLOUD_REGION}

.PHONY: infra-preview
infra-preview: infra-setup
	$(PULUMI) preview

# Preview + capture to infrastructure/preview.txt for the PR comment (INFRA workflow).
.PHONY: infra-diff
infra-diff: infra-setup
	$(PULUMI) preview --diff --non-interactive 2>&1 | tee preview.txt

.PHONY: infra-apply
infra-apply: infra-setup
	$(PULUMI) up --yes --skip-preview

# Full deploy: build+push image, then pulumi up. infra-setup is listed first (make
# dedups it against infra-apply's prereq → runs once) so a bad login/stack fails fast,
# before the docker build. Assumes `make setup` already ran on this box/container.
.PHONY: cd
cd: infra-setup backend-docker-push infra-apply
