# 0001 — Stack selection

- Status: **Accepted**
- Date: 2026-09-08

## Context

`rag-pipeline` must be a ready-to-go, portable RAG pipeline: runnable on a single laptop and
scalable to Kubernetes, with the same code. The stack is chosen to be mainstream enough to be
resume-relevant, and portable enough to avoid org-specific glue (no proprietary secret stores or
schedulers in the core).

## Decision

- **Language / tooling:** Python 3.12, `uv` (project + venv management), `ruff` (lint/format),
  `mypy` strict (typecheck), `pytest` (tests).
- **API:** FastAPI on `:8000` (async, pydantic-native, auto docs).
- **Worker / async:** Celery with a Redis broker + result backend; beat for scheduled cleanup.
- **Metadata store + vector index:** Postgres with `pgvector` (no SQLite).
- **Document vector store:** ChromaDB — embedded for zero-dependency dev, switchable to a
  server/remote Chroma for K8s (see OPEN D1).
- **Embeddings / LLM:** pluggable (`get_embedder`, `qa.py`) — **embeddings default to local
  Ollama `mxbai-embed-large`**; chat/QA synthesis defaults to **OpenRouter** free tier
  (`nvidia/nemotron-3-super-120b-a12b:free`). Both multi-provider behind `EMBEDDING_PROVIDER`
  / `LLM_PROVIDER` (see D2).
- **Retrieval + synthesis:** built in-house on the provider SDKs / HTTP APIs directly — no
  LangChain. A small `Embedder` protocol / `VectorStore` protocol / QA service wrap the providers
  (`ollama`, `openai`, `anthropic` SDKs and REST APIs) so every abstraction layer is ours.
- **Document parsing:** own parser service under `backend/app/services/` (self-contained;
  see `docs/adr/0002-document-parsing.md`).
- **Dashboard:** React + Vite + Zustand (dev `:5173`), Langfuse-style traces/usage/status.
- **Distribution:** Docker Compose (local dev) + raw K8s manifests via Kustomize (prod).

## Rejected alternatives

- **Nomad / a private registry / Vault-style secret stores** — org-specific, not portable; would
  obscure the K8s skill this project is meant to show instead.
- **SQLite** — cannot scale to the cluster / multi-worker story.
- **Serverless / managed vector DB (Pinecone/Milvus Cloud)** — good, but a managed external
  dependency conflicts with "deployable in any organization" and hides the vector-store skill.
  Kept as a documented switch, not the default.
- **Embedding/Pinecone as sole store, no pgvector** — need pgvector to keep metadata + vectors
  co-located and to show the Postgres skill.

## Consequences

- Portable: `docker compose up` on a laptop; same image + Kustomize manifests on K8s.
- All core orchestration is config-driven, satisfying the "customizable" capability.
