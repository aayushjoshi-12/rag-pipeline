.PHONY: help install backend-install frontend-install \
        backend-test backend-lint backend-fmt backend-typecheck backend-check \
        frontend-lint frontend-typecheck frontend-test frontend-check \
        check chroma-test-up chroma-test-down fmt \
        run run-backend run-frontend \
        db-up db-down migrate \
        up up-api down up-image down-image logs openapi \
        deploy deploy-dry images release-dry-run

# ── Help ──────────────────────────────────────────────────────────────────────

help:
	@echo "Targets:"
	@echo "  install        Sync backend + frontend deps"
	@echo "  check          Full quality gate: lint + typecheck + test (backend & frontend)"
	@echo "                 (starts a throwaway Chroma on :8002 for the tests and removes it afterwards)"
	@echo "  chroma-test-up/-down  Start/stop that throwaway Chroma by hand (a bare 'make backend-test' needs it)"
	@echo "  fmt            Apply ruff format to backend"
	@echo "  run            Run backend + frontend locally (no Docker)"
	@echo "  run-backend    Run FastAPI locally with reload (:8000)"
	@echo "  run-frontend   Run Vite dev server (:5173)"
	@echo "  db-up          Start db+redis+chroma only (compose)"
	@echo "  db-down        Stop db+redis+chroma"
	@echo "  migrate        Run alembic migrations against the dev DB"
	@echo "  up             Build + start the full compose stack"
	@echo "  up-api         Same, but API only (no dashboard container)"
	@echo "  down           Stop the compose stack"
	@echo "  openapi        Re-export docs/openapi.json (refuses unacknowledged breaking changes)"
	@echo "  logs           Tail compose logs"
	@echo "  images         Build backend + frontend images (REGISTRY, TAG, PLATFORM)"
	@echo "  release-dry-run  Build both images for the native platform, as the release workflow does (no push)"
	@echo "  up-image/down-image  Run the PUBLISHED images (docker-compose.image.yml; RAG_VERSION, RAG_IMAGE_REGISTRY)"
	@echo "  deploy         Apply a K8s overlay and wait for the migrate Job (ENV=dev|prod|kind|headless)"
	@echo "  deploy-dry     Dry-run the deploy step"

# ── Install ────────────────────────────────────────────────────────────────────

install: backend-install frontend-install

backend-install:
	cd backend && uv sync

frontend-install:
	@if [ -f frontend/package.json ]; then cd frontend && npm install; fi

# ── Checks ────────────────────────────────────────────────────────────────────

backend-test:
	cd backend && uv run pytest -q

backend-lint:
	cd backend && uv run ruff check .

backend-fmt: backend-fmt

backend-typecheck:
	cd backend && uv run mypy app

backend-check:
	@(cd backend && uv run ruff check .) & P1=$$!; (cd backend && uv run mypy app) & P2=$$!; (cd backend && uv run pytest -q) & P3=$$!; wait $$P1; R1=$$?; wait $$P2; R2=$$?; wait $$P3; R3=$$?; exit $$((R1 | R2 | R3))

frontend-lint:
	cd frontend && npm run lint

frontend-typecheck:
	cd frontend && npx tsc -b

frontend-test:
	cd frontend && npm run test:run

frontend-check:
	@(cd frontend && npm run lint) & P1=$$!; (cd frontend && npm run test:run) & P2=$$!; (cd frontend && npx tsc -b) & P3=$$!; wait $$P1; R1=$$?; wait $$P2; R2=$$?; wait $$P3; R3=$$?; exit $$((R1 | R2 | R3))

# Chroma-using tests run against a throwaway container (compose profile `test`, :8002, tmpfs), never the dev Chroma.
# `check` always tears it down, also when a check fails, and exits with the checks' status.
CHROMA_TEST_PORT ?= 8002

chroma-test-up:
	docker compose --profile test up -d chroma-test
	@for i in $$(seq 1 60); do curl -sf http://localhost:$(CHROMA_TEST_PORT)/api/v2/heartbeat >/dev/null && exit 0; sleep 1; done; \
	  echo "chroma-test did not become healthy on :$(CHROMA_TEST_PORT)"; exit 1

chroma-test-down:
	-docker compose --profile test rm --stop --force --volumes chroma-test

check:
	@$(MAKE) --no-print-directory chroma-test-up || { $(MAKE) --no-print-directory chroma-test-down; exit 1; }; \
	$(MAKE) --no-print-directory backend-check frontend-check; R=$$?; \
	$(MAKE) --no-print-directory chroma-test-down; exit $$R

openapi:
	cd backend && uv run python -m app.openapi_contract export

fmt:
	cd backend && uv run ruff format .

# ── Development ───────────────────────────────────────────────────────────────

run: run-backend run-frontend

run-backend:
	cd backend && uv run uvicorn app.main:app --reload --port 8000

run-frontend:
	cd frontend && npm run dev

# ── Database / deps (compose) ─────────────────────────────────────────────────

db-up:
	docker compose up -d db redis chroma

db-down:
	docker compose stop db redis chroma

migrate:
	cd backend && uv run alembic upgrade head

# ── Docker (full stack) ───────────────────────────────────────────────────────

# The compose `migrate` service runs alembic before api/worker start.
up:
	docker compose --profile ui up -d --build

# API only (headless): no dashboard container. Same stack otherwise.
up-api:
	docker compose up -d --build

down:
	docker compose --profile ui down

# The published images (no build): see docs/deployment.md. Pin RAG_VERSION=x.y.z for anything beyond a trial.
up-image:
	docker compose -f docker-compose.image.yml up -d

down-image:
	docker compose -f docker-compose.image.yml down

# Self-signed cert for trying HTTPS locally (./certs, gitignored). Use a real cert for anything else.
tls-selfsigned:
	mkdir -p certs
	openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=localhost" \
	  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
	  -keyout certs/privkey.pem -out certs/fullchain.pem
	chmod 644 certs/privkey.pem   # the container's nginx user must be able to read it; local use only

up-image-tls:
	docker compose -f docker-compose.image.yml -f docker-compose.tls.yml up -d

logs:
	docker compose logs -f api worker

# ── Deploy (Kubernetes) ─────────────────────────────────────────────────────────
# Applies the Kustomize overlay for the target env. Override with ENV=dev|prod.
# Prereq: a configured kubeconfig + image already pushed to a registry the
# cluster can pull from.

ENV ?= dev
REGISTRY ?= rag-pipeline
TAG ?= latest

# Build both images for linux/amd64 by default (required when building on Apple Silicon for an x86
# cluster). Override for a local arm64 cluster: `make images PLATFORM=linux/arm64 TAG=kind`.
PLATFORM ?= linux/amd64
images:
	docker build --platform $(PLATFORM) -t $(REGISTRY)/backend:$(TAG) backend
	docker build --platform $(PLATFORM) -f frontend/Dockerfile.prod -t $(REGISTRY)/frontend:$(TAG) frontend

# What .github/workflows/release.yml builds (same contexts and Dockerfiles), for the host's native platform, nothing pushed.
# Tagged rag-pipeline-{backend,frontend}:release-dry-run; amd64+arm64 is only built in CI (QEMU).
release-dry-run:
	docker build -t rag-pipeline-backend:release-dry-run backend
	docker build -f frontend/Dockerfile.prod -t rag-pipeline-frontend:release-dry-run frontend
	@docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | grep release-dry-run

# Jobs are immutable, so the previous migrate Job is removed before re-applying; then wait for it.
deploy:
	kubectl delete job rag-pipeline-migrate -n rag-pipeline --ignore-not-found
	kubectl apply -k k8s/overlays/$(ENV)
	kubectl wait --for=condition=complete job/rag-pipeline-migrate -n rag-pipeline --timeout=300s

deploy-dry:
	kubectl kustomize k8s/overlays/$(ENV)
