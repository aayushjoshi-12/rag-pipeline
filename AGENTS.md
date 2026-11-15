# AGENTS.md

Guidance for contributors and coding agents working in this repository. Humans should also read
[CONTRIBUTING.md](CONTRIBUTING.md). `CLAUDE.md` imports this file, so Claude Code and other agents share one source.

## Project

`rag-pipeline` is a ready-to-run, self-hostable Retrieval-Augmented Generation pipeline. It ingests
documents, parses them, chunks and embeds them, stores vectors in ChromaDB (or pgvector), and serves
search and chat (RAG) behind a FastAPI API and a React dashboard (traces, usage, cache, settings).
Celery workers run ingestion and vectorization asynchronously. The same code runs on one laptop with
Docker Compose and scales out on Kubernetes.

Capabilities:

- **Ready-to-run pipeline** with an opinionated default configuration.
- **Dashboard** with traces, usage, cost, cache and admin settings, and a Docker image story.
- **Compose for local dev, Kubernetes manifests for a cluster** (`k8s/`, kustomize overlays).
- **Celery workers** for ingestion, embedding and cleanup.
- **Document parser** (`backend/app/services/`, seam `parser.py`): PDF via OCR or vision backends, DOCX via
  docling to markdown. See [`docs/adr/0002-document-parsing.md`](./docs/adr/0002-document-parsing.md).
- **Customizable**: chunking, embedding models, vector stores, LLM providers and prompts are configuration.

## Running it

```bash
cp .env.example .env     # fill SECRET_KEY, SETTINGS_ENCRYPTION_KEY, ADMIN_EMAIL, ADMIN_PASSWORD
make up                  # Compose: api + worker + beat + migrate + db + redis + chroma (+ frontend, `ui` profile)
make check               # lint + typecheck + tests (starts and removes a throwaway Chroma)
make help                # every target
```

## Architecture

Stack is decided — see [`docs/adr/0001-stack.md`](./docs/adr/0001-stack.md).

Distributed monorepo with these runnable pieces:

- **`backend/`** — Python API (FastAPI on **:8000**), managed with `uv`
  (`backend/pyproject.toml`). Package lives at `backend/app/`; entry point `app.main:app`.
  Owns the RAG orchestration: ingest → parse → chunk → embed → store → query. Parsing is
  **fully config-driven** — vision provider (`openrouter` default → `openai`/`anthropic`/`ocr`),
  model, system prompt, PDF DPI, OCR lang, timeout — see
  [`docs/adr/0002-document-parsing.md`](./docs/adr/0002-document-parsing.md).
- **`frontend/`** — dashboard UI (React + Vite + Zustand, dev on **:5173**), Langfuse-style
  traces, usage, ingestion status, and document browsing.
- **`backend` Celery workers** — async consumption of the broker (Redis), running ingestion,
  parsing, embedding, and cleanup tasks.
- **Postgres + pgvector** — metadata store for documents/chunks and the vector index. No SQLite.
- **ChromaDB** — document vector store, always a Chroma server (the `chroma` Compose service /
  K8s Deployment, reached over HTTP via `chromadb-client`); `VECTOR_STORE=pgvector` is the alternative.
- **Redis** — Celery broker + result backend.
- **Observability** — optional Langfuse self-host (or cloud) for trace capture.

## Python

- `uv` manages everything: `make install`, `uv run ...` for ad-hoc commands (run from
  `backend/`).
- Quality gates before any commit: `make check` (lint → typecheck → test). Run all three; mypy
  is strict.
- Python 3.12+. Match existing style: `from __future__ import annotations`, full type
  annotations, pydantic models for API shapes.
- Run tests in the project `.venv`, not a global interpreter. Always go through `make` (or
  `uv run --extra dev`) — bare `pytest`/`ruff`/`mypy` can silently resolve to a global shim
  missing runtime deps. If `/ingest` returns 503 "worker is not initialized" but auth tests
  pass, suspect the interpreter before the code.

## Privacy

Documents may contain sensitive content. Raw uploads live only under `data/` / `tmp/`
(gitignored). Never commit document content — test fixtures must be synthetic.

## Directory map

```
.
├── AGENTS.md                    # contributor / coding-agent guide (this file); CLAUDE.md imports it
├── CONTRIBUTING.md  SECURITY.md  CODE_OF_CONDUCT.md  LICENSE
├── README.md
├── Makefile                     # install / check / fmt / run / db-up / migrate / up / up-api / down / openapi / deploy (see `make help`)
├── docker-compose.yml           # full local stack: api + worker + beat + migrate + db + redis + chroma (+ frontend via the `ui` profile)
├── .env.example                 # minimal env template; .env.reference lists every tunable
│
├── backend/                     # Python API + Celery workers (uv-managed)
│   ├── app/
│   │   ├── main.py              # FastAPI app + lifespan
│   │   ├── config.py            # pydantic-settings; three-layer config (env > config.json > defaults)
│   │   ├── celery_app.py        # Celery instance, broker/backend config, beat schedule
│   │   ├── http_common.py       # error envelope, X-Request-ID, /v1 mounting + deprecated aliases
│   │   ├── openapi_contract.py  # OpenAPI snapshot export + breaking-change check (make openapi)
│   │   ├── routers/             # route handlers (one file per resource), all mounted under /v1
│   │   │   ├── health.py        # GET /health — probes DB, Redis, Chroma, worker (unversioned)
│   │   │   ├── documents.py     # upload/list/get/delete documents, kick ingestion
│   │   │   ├── chat.py          # POST /chat, POST /chat/stream (SSE) — RAG query
│   │   │   ├── search.py        # POST /search — ranked passages, no LLM
│   │   │   ├── auth.py, api_keys.py, auth_deps.py  # login/RBAC, machine API keys, permission deps
│   │   │   ├── traces.py        # traces, usage, insights, credits
│   │   │   ├── cache.py         # GET /cache/stats, POST /cache/purge
│   │   │   └── capabilities.py  # GET /capabilities
│   │   ├── services/            # business logic — no HTTP knowledge
│   │   │   ├── ingestion.py     # orchestrate parse → chunk → embed → store
│   │   │   ├── parser.py        # parse seam: file → common ParseResult
│   │   │   ├── backends/        # text-extraction backends (openrouter openai anthropic ocr)
│   │   │   ├── chunking.py      # configurable chunking strategies
│   │   │   ├── embeddings.py    # get_embedder (ollama HTTP / openai SDK, custom protocol)
│   │   │   ├── vectorstore.py   # VectorStore protocol + Chroma server adapter (pgvector_store.py = pgvector)
│   │   │   ├── retriever.py     # semantic / keyword / hybrid retrieval + metadata filter
│   │   │   ├── search.py        # shared retrieve_ranked path (/search and /chat), neighbour context
│   │   │   ├── cache.py         # Redis query cache (embedding / retrieval / answer), ADR-0004
│   │   │   ├── llm.py           # providers: complete() + stream(), usage and cost capture
│   │   │   ├── storage.py, fetcher.py, urlguard.py  # local/S3 files, URL ingest, SSRF guard
│   │   │   ├── traces.py, usage_insights.py, credits.py  # trace log, usage aggregates, key balance
│   │   │   └── qa.py            # RAG prompt + answer synthesis (OpenRouter default)
│   │   ├── tasks/               # Celery task modules
│   │   │   ├── ingest.py        # async ingest pipeline
│   │   │   ├── cleanup.py       # retention/tmp cleanup (celery beat)
│   │   │   └── reindex.py       # vector reindex task
│   │   ├── db/                  # data layer
│   │   │   ├── models.py        # SQLAlchemy ORM: Document, Chunk, Job, Trace, User, Role, ApiKey
│   │   │   └── ops.py           # async DB ops
│   │   └── schema/              # pydantic request/response models
│   ├── config/
│   │   └── config.dev.json      # per-environment non-secret tunables (selected by RAG_ENV)
│   ├── tests/                   # pytest suite (mirrors app/ layout)
│   ├── alembic/                 # DB migrations
│   ├── pyproject.toml           # uv-managed; ruff/mypy/pytest config
│   └── Dockerfile               # backend image (uv + uvicorn + celery worker entrypoints)
│
├── frontend/                    # Dashboard UI (React + Vite + Zustand, dev :5173)
│   ├── src/
│   │   ├── api/client.ts
│   │   ├── App.tsx
│   │   ├── components/
│   │   ├── pages/               # Overview, Documents, Chat, Traces, Usage (Cost/Performance/RAG/Ingest/Cache), Admin (Users, Roles, API keys, Settings)
│   │   ├── store/               # Zustand stores
│   │   └── types/
│   ├── vite.config.ts           # Vite + proxy → backend :8000
│   └── package.json
│
├── k8s/
│   ├── base/                    # namespace, configmap, secrets, deployments, services, hpa
│   │   ├── kustomization.yaml
│   │   └── ...                  # api/deployment.yaml, worker/deployment.yaml, etc.
│   └── overlays/
│       ├── dev/                 # thin: 1 replica, Compose-like
│       ├── kind/                # local kind cluster (docs/kubernetes-local.md)
│       ├── headless/            # API only, no dashboard, behind an Ingress
│       └── prod/                # scaled: HPA, replicas, resource limits
│
├── config/                      # per-environment non-secret tunables (baked into image)
│   └── config.prod.json
├── context/                     # founding scope notes
│   └── v0/
│       └── README.md
├── scripts/                     # bench_cache.py (cache benchmark), verify_api_docs.py (runs docs/api.md examples)
├── docs/                        # index in docs/README.md
│   ├── adr/                     # 0001 stack · 0002 parsing · 0003 chunking · 0004 cache · 0005 /v1 contract · 0006 SSE · 0007 API keys · 0008 runtime settings
│   ├── api.md  configuration.md  architecture.md  performance.md  kubernetes-local.md  design/
│   └── openapi.json             # committed API contract (generated: make openapi); openapi-breaking-ack.json = approved breaks
├── data/                        # gitignored; runtime uploads/store
└── tmp/                         # gitignored; local scratch, never committed
```

## Config loading (dev vs prod)

Config is four-layered (highest wins): **env vars (incl. `.env`) → admin UI (database, `app_settings`) →
`config/config.<env>.json` → defaults** (ticket 43, ADR-0008). The UI layer is a pydantic-settings source
(`DbOverridesSource`) fed by an in-memory snapshot that `app/services/runtime_settings.py` loads from the DB
(API: lazily every `SETTINGS_REFRESH_SECONDS` and after its own writes; Celery: at task start), so any
`Settings()` already includes it. Never capture `Settings()` at import time for a UI-editable key; build it per
use. A setting pinned by an env var is shown locked in the UI; to manage it from the UI, unset the variable.
The registry (`app/services/settings_registry.py`) must classify every `Settings` field (a test fails
otherwise): `live` / `restart` / `reindex` / `bootstrap` (not UI-editable) and `secret`. UI-stored secrets are
Fernet-encrypted with `SETTINGS_ENCRYPTION_KEY` and write-only; secret values must never reach a response,
log, trace or audit row.

**Dev (local Compose / laptop)**: put infra URLs, `SECRET_KEY`, `SETTINGS_ENCRYPTION_KEY` and the bootstrap admin in `.env` (see `.env.example`; every tunable is listed in `.env.reference`). Everything else (providers, models, retrieval, cache, parser, storage) can be set in Admin > Settings; a key present in `.env` wins and shows as locked there. `RAG_ENV`
should be unset or `dev` — the config-file step uses `backend/config/config.dev.json` (run from `backend/`; `RAG_CONFIG_DIR` overrides the directory).

**Prod (K8s)**: `RAG_ENV=prod`; secrets come from K8s `Secret`s, non-secret tunables from
`config/config.prod.json` baked into the image (`/app/config/` in the container). `Settings()` resolves the
final values on construction (env > UI > JSON > defaults).

Secret fields (K8s Secret / `.env`-owned): provider keys (`OPENROUTER_API_KEY` default,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`), `DATABASE_URL`, `REDIS_URL`, `CHROMA_*` creds,
`SECRET_KEY`, `CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND`.

Non-secret tunables (JSON-owned): `RAG_ENV`, `PORT`, `LOG_LEVEL`, `EMBEDDING_PROVIDER`,
`EMBEDDING_MODEL`, `OLLAMA_BASE_URL`, `LLM_PROVIDER`,
`DEFAULT_LLM_MODEL`, `CHUNK_SIZE`, `CHUNK_OVERLAP`, `VECTOR_STORE`, `DEFAULT_TOP_K`,
`MAX_UPLOAD_SIZE_BYTES`, `LLM_MAX_TOKENS`, `EMBEDDING_PRICE_PER_MTOK`, `CACHE_*` (query cache, ADR-0004), `CORS_ALLOW_ORIGINS`,
`RETRIEVAL_*`, `VECTOR_*`/`HNSW_*`, `STORAGE_BACKEND`/`S3_*` (the S3 keys are secrets), plus all `PARSER_*` / `OPENROUTER_BASE_URL` / `PDF_DPI` / `OCR_LANG`
/ `DOCX_READER` parse knobs (see ADR-0002).

## Quality gates

`make check` runs lint + typecheck + tests. **Run it before every commit or pull request.**

```bash
make check    # ruff check + mypy + pytest
make fmt      # ruff format (apply, not just check)
```

Don't merge with failing gates. Don't add `# noqa` / `# type: ignore` without a one-line reason
in a comment.

Coverage target: 70% on new code. Aim for it.

### Test conventions

- **Unit tests** (`backend/tests/unit/`): pure-function logic, isolated services with fakes.
- **Integration tests** (`backend/tests/integration/`): FastAPI `TestClient`/`httpx` against a
  real throwaway Postgres (never SQLite, never the dev `rag` DB), the lifespan actually runs. Replace external clients
  (LLM, Chroma, Redis) with fakes in test setup — not by monkeypatching modules.
- Suites run with `CACHE_ENABLED=false` (the query cache lives in a shared Redis); cache tests opt in on Redis db 13 and use a real Redis on purpose (invalidation, TTL and fail-open are the behaviour under test).
- **Chroma tests use a throwaway Chroma, never the dev one.** `make check` starts `chroma-test` (Compose profile `test`,
  :8002, tmpfs, pinned image, memory-capped), runs everything and always removes it; for a bare `make backend-test`
  run `make chroma-test-up` first. Tests read `CHROMA_TEST_HOST`/`CHROMA_TEST_PORT` (default `localhost:8002`);
  `tests/conftest.py` refuses any other Chroma address and, if the server is unreachable, **fails** loudly (skips
  only with `RAG_TEST_ALLOW_NO_CHROMA=1`). Reason: Chroma keeps every written index in memory, and tests that
  churned the dev server grew it ~350 MiB per run (ticket 50).
- **Don't make real API calls in tests** (no live OpenAI/Anthropic/Chroma/Redis). Use
  `pytest-httpx`, `MagicMock`, or in-memory fakes.

When you add a route or service, add a unit test and an integration test covering the happy
path.

## Commit and branch conventions

- Branch names: `feat/<short>`, `fix/<short>`, `chore/<short>`, `docs/<short>`, `test/<short>`, `refactor/<short>`.
- Conventional Commits in the title: `feat(api): ...`, `fix(deploy): ...`, `docs(readme): ...`.
- Create new commits rather than amending shared history.

## Cost / risk gotchas

- **`POST /chat` and ingestion call the LLM / embedding provider on every call.** Default dev
  limits are low. Batch/ingest with a small document for any first test — don't trigger a large
  ingest without considering cost.
- **Building on Apple Silicon needs `--platform linux/amd64`** if the target is an x86 cluster.
  Baked into deploy scripts; don't remove it.
- **Single-quote secrets in `.env*` files.** Bash will silently mangle values containing `&`,
  `;`, `!`, `$`, etc. on `source`.

## Where to read more

| Question | Doc |
|---|---|
| Scope of the project | `context/v0/README.md` |
| Stack decision + rejected alternatives | `docs/adr/0001-stack.md` |
| How does parsing work? | `docs/adr/0002-document-parsing.md` + `backend/app/services/parser.py` |
| Where is everything documented? | `docs/README.md` (index) |
| Query cache, invalidation, numbers | `docs/adr/0004-query-cache.md` + `docs/performance.md` |
| API versioning, error envelope, OpenAPI contract | `docs/adr/0005-api-versioning-and-contract.md` + `docs/api.md` |
| Streaming chat / API keys | `docs/adr/0006-chat-streaming-sse.md`, `docs/adr/0007-api-keys.md` |
| Runtime (admin UI) settings | `docs/adr/0008-runtime-settings.md` |
| Every setting and its default | `docs/configuration.md` |
