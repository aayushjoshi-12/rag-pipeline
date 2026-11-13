.PHONY: help install backend-install frontend-install \
        backend-test backend-lint backend-fmt backend-typecheck backend-check \
        frontend-lint frontend-typecheck frontend-test frontend-check \
        check fmt \
        run run-backend run-frontend \
        db-up db-down migrate \
        up down logs \
        deploy deploy-dry images

# ── Help ──────────────────────────────────────────────────────────────────────

help:
	@echo "Targets:"
	@echo "  install        Sync backend + frontend deps"
	@echo "  check          Full quality gate: lint + typecheck + test (backend & frontend)"
	@echo "  fmt            Apply ruff format to backend"
	@echo "  run            Run backend + frontend locally (no Docker)"
	@echo "  run-backend    Run FastAPI locally with reload (:8000)"
	@echo "  run-frontend   Run Vite dev server (:5173)"
	@echo "  db-up          Start db+redis+chroma only (compose)"
	@echo "  db-down        Stop db+redis+chroma"
	@echo "  migrate        Run alembic migrations against the dev DB"
	@echo "  up             Build + start the full compose stack"
	@echo "  down           Stop the compose stack"
	@echo "  logs           Tail compose logs"
	@echo "  deploy         Build/push/apply K8s manifests (ENV=dev|prod)"
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

backend-fmt:
	cd backend && uv run ruff format .

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

check: backend-check frontend-check

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
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f api worker

# ── Deploy (Kubernetes) ─────────────────────────────────────────────────────────
# Applies the Kustomize overlay for the target env. Override with ENV=dev|prod.
# Prereq: a configured kubeconfig + image already pushed to a registry the
# cluster can pull from.

ENV ?= dev
REGISTRY ?= rag-pipeline
TAG ?= latest

# Build both images for linux/amd64 (required when building on Apple Silicon for an x86 cluster).
images:
	docker build --platform linux/amd64 -t $(REGISTRY)/backend:$(TAG) backend
	docker build --platform linux/amd64 -f frontend/Dockerfile.prod -t $(REGISTRY)/frontend:$(TAG) frontend

# Jobs are immutable, so the previous migrate Job is removed before re-applying; then wait for it.
deploy:
	kubectl delete job rag-pipeline-migrate -n rag-pipeline --ignore-not-found
	kubectl apply -k k8s/overlays/$(ENV)
	kubectl wait --for=condition=complete job/rag-pipeline-migrate -n rag-pipeline --timeout=300s

deploy-dry:
	kubectl kustomize k8s/overlays/$(ENV)
