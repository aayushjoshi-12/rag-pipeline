# CLAUDE.md

Operating rules for Claude (or any agent) working in this repo. Read this before doing anything.

## Project

`rag-pipeline` — a ready-to-go, enterprise-grade Retrieval-Augmented Generation pipeline that
is **scalable, customizable, and deployable in any organization or personal environment** (and
a resume centerpiece). It ingests documents → parses them (ChromaDB vector store) → indexes
them → serves chat/RAG queries behind a FastAPI API and a Langfuse-style dashboard, with
Celery workers handling async ingestion and vectorization.

The core capabilities this repo exists to demonstrate:

- **Ready-to-go RAG pipeline** — a runnable, opinionated default configuration out of the box.
- **Dashboard + Docker image, Langfuse-style** — an observability/trace/usage dashboard and a
  clean, single-image deploy story.
- **Scalable + deployable on Kubernetes** — distribution that runs on Compose for local dev and
  K8s for production.
- **Celery workers** — async, horizontally-scalable ingestion/embedding/cleanup workers.
- **Document parser** — original, self-contained parse service (`backend/app/services/`,
  seam `parser.py`); PDF via OCR/vision backends, DOCX via docling → markdown. See
  [`docs/adr/0002-document-parsing.md`](./docs/adr/0002-document-parsing.md).
- **Customizable** — pluggable config for chunking, embedding models, vector stores, LLM
  providers, and prompt templates.

This is a **personal / portfolio** project — not org-internal infrastructure. It must be able
to run on a single laptop (Compose) and scale out to a cluster (K8s) with the same code.

## Action Guards

**Never do any of the following without explicit instruction:**
- `git commit` — finish the code and quality checks, then stop and wait
- `git push`
- Any other operation that affects shared state (deploys, registry push, cluster apply)

Present the finished work and let the user decide when to commit, push, and deploy.

## Dev workflow (tickets → plans → handoff → smoke test)

This is how we work — every task follows this cycle. All artifacts live under `tmp/` (repo-local,
gitignored).

1. **Tickets.** Work starts from `tmp/tickets/` — a local Jira. Each ticket file holds the task,
   a decent description, its status, and **acceptance criteria (non-negotiable)**. Statuses: a
   ticket is either in one file or listed in a status index — keep them actionable (e.g.
   `open` / `in_progress` / `blocked` / `done`). Start a ticket only by working from its file;
   before starting work, check the ticket is up to date. Draft any newly-discovered scope as a
   new ticket file under `tmp/tickets/` rather than scope-creeping the current one.
2. **Plans.** For each ticket, either the user provides a plan in `tmp/plans/` or explicitly
   instructs the agent to create one. Write the plan **before** touching code
   (`tmp/plans/<ticket>-<title>.md`), then implement it, adjusting as reality diverges.
3. **Implement.** Work the plan. Keep changes scoped to the ticket; run `make check` before any
   commit.
4. **Smoke test (done gate).** **No task is `done` until a manual smoke test passes** in
   `tmp/smoke_tests/` using **real credentials** — real DB, real running server, real external
   APIs, real curl/http requests, nothing mocked on the changed path. This is **separate from
   the automated tests in `tests/`** and never runs in CI. Script:
   `tmp/smoke_tests/<ticket>-<title>-smoke.py|sh` (+ `-result.md` report). Report must include:
   what it ran against (real creds from `.env`, prereqs like `make up` + migrate), script/curl
   reference, a `| Check | Result |` table, an explicit **Overall: ALL PASS** / **FAILED** line,
   bugs found & fixed, and confirmation that `make check` is green. Never commit/push while the
   smoke test or `make check` is red — both green is the commit gate, on top of the action
   guards above.
5. **Handoff.** Write a handoff in `tmp/handoff/` **only once everything is green** (smoke test
   + `make check` both pass). Write it when:
   - a task is **completed** so the next task starts with prior context, or
   - the agent harness runs out of context window.
   Handoffs are for a fresh chat: include what was completed (with current state), key findings,
   repo state, patterns to mirror, explicit don'ts, and — if the task is done — the **next best
   step** to pick up. Name: `tmp/handoff/<ticket>-<title>.md`. Under an epic, stopping this
   session after the handoff and starting the next ticket in a fresh chat is the intended flow.

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
- **ChromaDB** — document vector store (embedded by default for zero-dependency dev, switchable
  to a server/remote Chroma for K8s).
- **Redis** — Celery broker + result backend.
- **Observability** — optional Langfuse self-host (or cloud) for trace capture.

## Commits

- Use `git config` locally — author is **Aayush Joshi**.
- Don't amend; create new commits.
- Always run `make check` (lint + typecheck + test) before committing. Fix all failures first.

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
├── CLAUDE.md
├── README.md
├── Makefile                     # install / lint / typecheck / test / check / run / up / down
├── docker-compose.yml           # full local stack: api + worker + db + redis + chroma + frontend
├── .env.example                 # root env template (LLM keys, DB, Redis, broker, ports)
├── Dockerfile                   # backend API/worker image (uv multi-stage; see backend/Dockerfile)
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
│   │   │   ├── vectorstore.py   # Chroma store adapter (embedded ↔ server)
│   │   │   ├── retriever.py     # semantic / keyword / hybrid retrieval + metadata filter
│   │   │   ├── search.py        # shared retrieve_ranked path (/search and /chat), neighbour context
│   │   │   ├── cache.py         # Redis query cache (embedding / retrieval / answer), ADR-0004
│   │   │   ├── llm.py           # providers: complete() + stream(), usage and cost capture
│   │   │   ├── storage.py, fetcher.py, urlguard.py  # local/S3 files, URL ingest, SSRF guard
│   │   │   ├── traces.py, usage_insights.py, credits.py  # trace log, usage aggregates, key balance
│   │   │   └── qa.py            # RAG prompt + answer synthesis (OpenRouter default)
│   │   ├── tasks/               # Celery task modules
│   │   │   ├── ingest.py        # async ingest pipeline
│   │   │   └── cleanup.py       # retention/tmp cleanup, reindex
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
│   │   ├── pages/               # Overview, Documents, Chat, Traces, Usage (Cost/Performance/RAG/Ingest/Cache), Admin
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
│       └── prod/                # scaled: HPA, replicas, resource limits
│
├── config/                      # per-environment non-secret tunables (baked into image)
│   └── config.prod.json
├── context/                     # founding context (see below)
│   └── v0/
│       └── README.md
├── scripts/                     # bench_cache.py (cache benchmark), verify_api_docs.py (runs docs/api.md examples)
├── docs/                        # index in docs/README.md
│   ├── adr/                     # 0001 stack · 0002 parsing · 0003 chunking · 0004 cache · 0005 /v1 contract · 0006 SSE · 0007 API keys
│   ├── api.md  configuration.md  architecture.md  performance.md
│   └── openapi.json             # committed API contract (generated: make openapi); openapi-breaking-ack.json = approved breaks
├── data/                        # gitignored; runtime uploads/store
└── tmp/                         # gitignored; dev-workflow artifacts (all scratch, never committed)
    ├── tickets/                 # local Jira — ticket + status + acceptance criteria
    ├── plans/                   # per-ticket implementation plans
    ├── handoff/                 # session handoffs for fresh-chat pickup
    └── smoke_tests/             # manual smoke tests w/ real creds + result reports
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

**Dev (local Compose / laptop)**: set everything in `.env` (see `.env.example`). `RAG_ENV`
should be unset or `dev` — the config-file step uses `config/config.dev.json`.

**Prod (K8s)**: `RAG_ENV=prod`; secrets come from K8s `Secret`s, non-secret tunables from
`config/config.prod.json` baked into the image. `Settings._apply_env()` on boot resolves the
final values.

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

`make check` runs lint + typecheck + tests. **Run it before every commit.**

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
- **Don't make real API calls in tests** (no live OpenAI/Anthropic/Chroma/Redis). Use
  `pytest-httpx`, `MagicMock`, or in-memory fakes.

When you add a route or service, add a unit test and an integration test covering the happy
path.

## Commit + branch conventions

- Branch naming: `feat/<short>`, `fix/<short>`, `chore/<short>`, `docs/<short>`,
  `test/<short>`, `refactor/<short>`.
- Conventional Commits in the title: `feat(api): ...`, `fix(deploy): ...`, `docs(readme): ...`.
- Don't push to `origin/main` or deploy without explicit confirmation.

## Cost / risk gotchas

- **`POST /chat` and ingestion call the LLM / embedding provider on every call.** Default dev
  limits are low. Batch/ingest with a small document for any first test — don't trigger a large
  ingest without considering cost.
- **Building on Apple Silicon needs `--platform linux/amd64`** if the target is an x86 cluster.
  Baked into deploy scripts; don't remove it.
- **Single-quote secrets in `.env*` files.** Bash will silently mangle values containing `&`,
  `;`, `!`, `$`, etc. on `source`.

## When to ask vs proceed

- **Proceed silently** for: code changes that follow established patterns, tests for new code,
  doc updates, lint/format/typecheck fixes, dry-runs, reads.
- **Confirm with the user before**: real deploys, `kubectl apply`, `git push origin main`,
  modifying `.env` / `.env.deploy`, deleting committed files, adding a new top-level dependency,
  changing the LLM model or prompt, dropping a test.
- **Surface, don't decide**: if a design decision has user-facing tradeoffs (e.g., which vector
  store, what chunking default, dev defaults), present options and pick after they choose.

## Pointers to deeper context

| Question | Doc |
|---|---|
| Why does this repo exist / what's the scope? | `context/v0/README.md` |
| Stack decision + rejected alternatives | `docs/adr/0001-stack.md` |
| How does parsing work? | `docs/adr/0002-document-parsing.md` + `backend/app/services/parser.py` |
| Where is everything documented? | `docs/README.md` (index) |
| Why a query cache, how is it invalidated, what are the numbers? | `docs/adr/0004-query-cache.md` + `docs/performance.md` |
| API versioning, error envelope, OpenAPI contract? | `docs/adr/0005-api-versioning-and-contract.md` + `docs/api.md` |
| Streaming chat / API keys? | `docs/adr/0006-chat-streaming-sse.md`, `docs/adr/0007-api-keys.md` |
| Every setting and its default? | `docs/configuration.md` |
