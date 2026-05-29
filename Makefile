include .env
export

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
	uv sync
	uv run pre-commit install --hook-type pre-commit --hook-type pre-push

.PHONY: backend-run
backend-run:
	@fuser -k 8080/tcp 2>/dev/null; true
	@mkdir -p ~/Logs tmp
	nohup uv run python -m app.backend > ~/Logs/nisse-backend.log 2>&1 < /dev/null & disown
	@ln -sf ~/Logs/nisse-backend.log tmp/backend.log
	@echo "Backend started (logs: tmp/backend.log → ~/Logs/nisse-backend.log)"

.PHONY: backend-dry-run
backend-dry-run:
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

.PHONY: test
test: lint typecheck backend-dry-run

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
.PHONY: infra-setup
infra-setup:
	$(PULUMI) login gs://${PULUMI_STATE_BUCKET}
	$(PULUMI) stack select prod
	$(PULUMI) config set gcp:project ${GOOGLE_CLOUD_PROJECT}
	$(PULUMI) config set gcp:region ${GOOGLE_CLOUD_REGION}

.PHONY: infra-preview
infra-preview:
	$(PULUMI) preview

.PHONY: infra-apply
infra-apply:
	$(PULUMI) up --yes --skip-preview

.PHONY: cd
cd: backend-docker-push infra-apply
