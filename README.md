# rag-pipeline

An **enterprise-grade, ready-to-go RAG pipeline** — scalable, customizable, and deployable in
any organization or personal environment. Turn documents into a queryable knowledge base:
ingest → parse → chunk → embed → store → retrieve → answer, behind a FastAPI API, async
Celery workers, and a Langfuse-style dashboard.

## Features

- **Ready-to-go RAG pipeline** — opinionated, runnable defaults out of the box.
- **Dashboard + Docker image, Langfuse-style** — traces, usage, ingestion status; one-command
  local stack.
- **Scalable + deployable on Kubernetes** — same image/manifests on Compose (dev) and K8s (prod)
  with HPA.
- **Celery workers** — async ingestion/embedding/cleanup that scale horizontally.
- **Document parser** — original, self-contained (`backend/app/services/`); PDF via
  OCR/vision backends, DOCX via docling → markdown. Every
  knob configurable (provider, model, system prompt, DPI, OCR lang).
- **Customizable** — pluggable chunking, embedding models, LLM providers, vector store, prompts,
  and parser behavior.

## Stack

OpenRouter (chat + vision parsing, free tier) · FastAPI · Celery + Redis · Postgres + pgvector ·
ChromaDB · React + Vite. Embeddings via Ollama (`mxbai-embed-large`), providers wired through our
own thin SDK/HTTP adapters (no LangChain). Managed with `uv`. See
[docs/adr/0001-stack.md](docs/adr/0001-stack.md) for rationale.

## Quick start (local)

Requires: Docker, `uv`, `node`.

```bash
cp .env.example .env        # fill in OPENROUTER_API_KEY (vision parsing)
make install                # uv sync + npm install
docker compose up -d        # full stack: api, worker, db, redis, chroma, frontend
make migrate                # alembic upgrade head
```

- API: http://localhost:8000 (docs at `/docs`)
- Dashboard: http://localhost:5173

Run without Docker:

```bash
make db-up                  # db + redis + chroma only
make run-backend            # uvicorn :8000
make run-frontend           # vite :5173
```

## Quality gates

```bash
make check                  # ruff + mypy + pytest (backend) + lint/typecheck/test (frontend)
```

Never commit with failing gates.

## Deploy to Kubernetes

```bash
make deploy ENV=dev        # kubectl apply -k k8s/overlays/dev
make deploy ENV=prod       # includes HPA + resource limits
```

See [CLAUDE.md](CLAUDE.md) for config loading, action guards, and operating rules; see
[context/v0/README.md](context/v0/README.md) for founding scope and OPEN decisions.