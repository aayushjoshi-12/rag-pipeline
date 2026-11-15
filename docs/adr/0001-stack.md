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
- **Document vector store:** ChromaDB — planned as embedded for dev and a server for K8s (see OPEN D1);
  as built it is always the Chroma server (see the 2026-11-15 addendum).
- **Embeddings / LLM:** pluggable (`get_embedder`, `qa.py`) — **embeddings default to local
  Ollama `mxbai-embed-large`**; chat/QA synthesis defaults to **OpenRouter** free tier
  (`nvidia/nemotron-3-ultra-550b-a55b:free`). Both multi-provider behind `EMBEDDING_PROVIDER`
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

## Addendum (2026-11-14): vector store is switchable

The `VectorStore` protocol now has two backends behind `VECTOR_STORE`: **Chroma** (still the default and
unchanged when nothing is set) and **pgvector** (a dedicated `vector_items` table in the app's Postgres, with HNSW,
IVFFlat or no index, and a cosine/l2/ip metric). Both pass one shared contract-test suite against real services.
Switching the store, metric, index type or embedding dimension on existing data is an explicit
`python -m app.manage reindex` (re-embeds from the stored chunk text); the app detects the drift and says so instead of
returning quietly wrong results. The earlier plan to keep chunk vectors in `chunks.vector` was dropped: chunk rows are
replaced wholesale on every ingest and that column was a fixed `vector(1024)`. See `docs/configuration.md`.

## Addendum (2026-11-15): what changed around the original stack

- **File storage** is pluggable: local disk (default) or S3 (the Floci AWS emulator locally; it replaced MinIO), plus ingest from a URL or an `s3://` link. See `docs/configuration.md`.
- **Redis also hosts the query cache** (own db 12, fail-open): [ADR-0004](0004-query-cache.md). API contract and `/v1`: [ADR-0005](0005-api-versioning-and-contract.md). Streaming chat: [ADR-0006](0006-chat-streaming-sse.md). API keys: [ADR-0007](0007-api-keys.md). Admin Settings window (UI settings layer): [ADR-0008](0008-runtime-settings.md).
- **Chroma is never embedded.** The app uses `chromadb.HttpClient` (the thin `chromadb-client` package) against the `chroma` service; no in-process Chroma mode exists.
- **The default chat model is a configuration choice, not part of this decision.** The free `nvidia/nemotron-*:free` default named above often returns empty answers (a reasoning model spending its output budget); the model is set with `DEFAULT_LLM_MODEL`, and a tiny non-reasoning model is used when cost reporting is under test.

