# Initial concept — v0

## Premise

`rag-pipeline` is a **ready-to-go, enterprise-grade Retrieval-Augmented Generation pipeline**
that is **scalable, customizable, and deployable in any organization or personal environment**.
It is a personal / portfolio project meant to demonstrate the exact skills a production RAG
engineering role asks for — which doubles as shippable, reusable infrastructure.

Unlike org-internal services bound to a specific tenant and deployment, `rag-pipeline` must be
**portable**. The same code runs on a single laptop via Docker Compose and
scales out to a multi-replica Kubernetes cluster — no org-specific glue allowed in the core.

**This is the product.** It is not a tenant of anything; it is the reusable platform itself.

## User

Any team (or individual) that wants a working RAG pipeline without building one from scratch:
upload documents → get them parsed, chunked, embedded, and indexed → query them through a
chat API and a Langfuse-style dashboard. Plus anyone wanting to read/fork a clean, opinionated
reference implementation.

## Job to be done

Give an organization a single place to turn documents into a queryable knowledge base —
parsing, chunking, embedding, vector storage, retrieval, and answer synthesis behind a
FastAPI API, async Celery ingestion workers, and an observability dashboard — runnable with
one command locally and deployable to Kubernetes with the same code.

## Core entities

- `document` — the raw uploaded file (PDF/DOCX/TXT/MD)
- `chunk` — a parsed, split unit of text (with metadata) that gets embedded
- `embedding` — the vector representation of a chunk (pgvector + ChromaDB)
- `job` — an async ingestion run (status, progress, errors)
- `trace` — an observability record of a query/ingest (Langfuse-style)

## Pipeline (ingest → query)

```
upload → parse → chunk → embed → store (ChromaDB) → index (pgvector)
                                                                              ↓
POST /chat → retrieve (similarity + metadata filter) → synthesize (LLM) → answer + sources
```

## Scope

An opinionated, functional RAG pipeline: a FastAPI backend, async Celery ingestion/embedding
workers, Postgres+pgvector metadata, ChromaDB vector store, Redis broker, a React+Vite
dashboard, and a clean Docker Compose + Kubernetes distribution. No org-specific integration —
LLM providers, embedding models, chunking, vector store, and prompt templates are pluggable
config.

## Out of scope (explicit)

- No tenant-specific business logic, schemas, or document layouts — everything is
  caller/generic.
- No phase-1 chart/visualization analytics beyond the dashboard's core traces/usage/status.
- No cross-repo imports — the parser is original code in `backend/app/services/` so the
  pipeline is self-contained and deployable anywhere (see `docs/adr/0002-document-parsing.md`).

## Stack

Defaults: uv + ruff + pytest + mypy, Python 3.12, FastAPI, Celery + Redis, Postgres + pgvector,
ChromaDB, in-house retrieval + synthesis on provider SDKs/HTTP (no LangChain), React + Vite +
Zustand frontend. See `docs/adr/0001-stack.md` for rationale and rejected alternatives.

## Deploy target

- **Local / dev**: Docker Compose — `api`, `worker`, `db` (pgvector), `redis`, `chroma`,
  `frontend`.
- **Prod**: Kubernetes (raw manifests via Kustomize under `k8s/`) with multiple replicas,
  HPA, and resource limits — the headline "scalable + deployable on Kubernetes" capability.

## Success criteria for v1

1. **`docker compose up`** boots the full stack; uploading a small document ingests it
   (parse → chunk → embed → store) and `POST /chat` answers from it with cited sources.
2. Ingestion runs **asynchronously** on Celery workers with durable job status surfaced in the
   dashboard; workers scale horizontally with no code change.
3. The same image / manifests deploy to **Kubernetes** with N replicas and HPA.
4. Chunking, embedding model, LLM provider, vector store, and prompt template are all
   **configurable** without code edits.
5. Traces, usage, and ingestion status are visible in the **Langfuse-style dashboard**.

## OPEN decisions

These are unresolved. Record them here rather than answer them; any task that depends on one
must gate on it rather than invent a default:

- **Vector store default / flexibility (D1):** ChromaDB is embedded for zero-dependency dev,
  but postgres-pgvector is the metadata store AND a candidate vector index. Decide whether v1
  uses ChromaDB (embedded dev → server prod) or pgvector for both metadata and vectors — this
  shapes `service/vectorstore.py` and the K8s manifests. Working assumption: ChromaDB embedded
  dev / server prod, pgvector for vectors if local-first ergonomics win.
- **LLM provider defaults (D2 — updated):** embeddings default to **Ollama local
  `mxbai-embed-large`** (already installed locally). Chat/QA answer synthesis and document
  parsing both default to **OpenRouter** free tier (no OpenAI/Anthropic keys): chat →
  `nvidia/nemotron-3-super-120b-a12b:free`, PDF vision → `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`.
  All multi-provider behind `EMBEDDING_PROVIDER` / `LLM_PROVIDER` / `PARSER_PROVIDER`.
- **Document parser (D3 — answered, see `docs/adr/0002-document-parsing.md`):** an original
  parse service in `backend/app/services/` — PDF page renderer, Tesseract OCR backend, OpenAI/
  Anthropic **vision text-extraction backends**, typed errors + retry helper, and Langfuse
  tracing. **DOCX parsing uses `docling` → markdown.** The default vision provider is an
  **OpenRouter backend** (free tier, no OpenAI/Anthropic keys). No structured field extraction.
  **Every parsing knob is runtime-configurable** — provider, model, system prompt, PDF DPI, OCR
  lang, tesseract path, timeout.
