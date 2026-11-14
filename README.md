# rag-pipeline

A self-hostable **Retrieval-Augmented Generation pipeline**: upload documents, have them parsed,
chunked, embedded and indexed by background workers, then ask questions and get answers with
cited sources. FastAPI + Celery + Postgres/pgvector + ChromaDB + a React dashboard, runnable on a
laptop with Docker Compose and shaped for Kubernetes (Kustomize).

```
upload ─► API (FastAPI) ─► Redis ─► Celery worker ─► parse ─► chunk ─► embed ─► Chroma + Postgres
                                                                                     │
question ─► API ─► embed query ─► Chroma similarity ─► LLM (grounded prompt) ─► answer + sources
```

## What it does

- **Ingest** — `POST /documents` (pdf, docx, txt, md). The API only stores the file and enqueues a
  job; a worker does the heavy lifting and reports status you can poll (`GET /jobs/{id}`).
- **Parse** — PDFs use their own text layer when they have one (free, instant, no API key); only
  scanned pages are rendered and read by a vision LLM (OpenRouter / OpenAI / Anthropic) or Tesseract OCR; DOCX goes through docling's Word backend
  to markdown (no ML models). Provider, model, system
  prompt, DPI, OCR language and timeouts are all config ([ADR-0002](docs/adr/0002-document-parsing.md)).
- **Chunk** — content-defined chunking (rolling hash), so editing one paragraph leaves the other
  chunks — and their SHA-256 hashes — unchanged ([ADR-0003](docs/adr/0003-chunking.md)).
- **Answer** — `POST /chat` retrieves the nearest chunks (optionally limited to chosen documents),
  builds a numbered-context prompt and returns the answer plus the sources it used. If nothing
  relevant is found the LLM is not called.
- **Search without an LLM** — `POST /search` returns the ranked passages only (same retrieval as `/chat`, no LLM key
  needed), for teams that bring their own UI or LLM. See [docs/api.md](docs/api.md).
- **Observe** — Langfuse spans for every ingest stage and chat request; prompt/question content is
  only sent when `LANGFUSE_CAPTURE_CONTENT=true`. Structured JSON logs carry `job_id` / `chat_id`.
- **Dashboard** — upload with live job status, document list/detail with chunks, chat with citations,
  plus a Traces page (every chat and ingest run: status, duration, model, tokens, hits, error) and a
  Usage page (requests, error rate, latency and LLM tokens per day). Question text is only stored in
  traces when `TRACE_CAPTURE_CONTENT=true`; traces are purged after `TRACE_RETENTION_DAYS` (30).
- **Access control** — JWT login with role-based access: admins define roles as sets of permissions
  (`documents:read|write|delete`, `jobs:read`, `chat:use`, `search:use`, `traces:read`, `users:manage`, `roles:manage`) and assign
  them to users. Permissions are checked server-side on every request and re-read from the database,
  so editing a role or deactivating a user takes effect immediately.
- **Housekeeping** — a Celery beat task removes orphaned upload directories.

## Quick start (Docker Compose)

Requires Docker and [Ollama](https://ollama.com) on the host (embeddings), plus a free
[OpenRouter](https://openrouter.ai) key for chat.

```bash
ollama pull mxbai-embed-large
cp .env.example .env            # set OPENROUTER_API_KEY, SECRET_KEY, ADMIN_EMAIL, ADMIN_PASSWORD
                                # (single-quote values containing special characters)
docker compose up -d --build    # db, redis, chroma, migrate (one-shot), api, worker, beat, frontend
```

- Dashboard: http://localhost:5173 &nbsp;·&nbsp; API docs: http://localhost:8000/docs
- Sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD` (the first admin is created only while there are no
  users). Locked out? `docker compose exec api python -m app.manage create-admin --email you@example.com`.
  `AUTH_ENABLED=false` disables login entirely (every request is an admin) — demos only.

Inside the containers the service hostnames (`db`, `redis`, `chroma`) and the host Ollama
(`host.docker.internal`) override whatever `.env` says, so the same `.env` works for host-run
development and for Compose.

### Without containers for the app

```bash
make install        # uv sync + npm install
make db-up          # postgres, redis, chroma only
make migrate
make run-backend    # :8000        (worker: uv run celery -A app.celery_app worker, from backend/)
make run-frontend   # :5173
```

## Configuration

Three layers, highest wins: environment variables → `config/config.<RAG_ENV>.json` → defaults
(`backend/app/config.py`). Secrets (API keys, `DATABASE_URL`, …) live in `.env` / Kubernetes
Secrets; tunables (models, chunk size, top-k, parser knobs) are plain config. Everything is
swappable: `LLM_PROVIDER` (openrouter | openai | anthropic), `EMBEDDING_PROVIDER` (ollama | openai),
`PARSER_PROVIDER` (openrouter | openai | anthropic | ocr). Free-tier OpenRouter models come and go —
if chat returns a 502, check `DEFAULT_LLM_MODEL` against OpenRouter's current model list.
`VECTOR_STORE` (chroma | pgvector), `VECTOR_METRIC`, `VECTOR_INDEX` (hnsw | ivfflat | none) and the HNSW / IVFFlat
knobs choose where vectors live and how they are indexed; changing them on existing data is a
`python -m app.manage reindex`. `RETRIEVAL_MODE` (semantic | keyword | hybrid, also per request on `POST /chat`),
`RETRIEVAL_KEYWORD_METHOD` (bm25 | postgres_fts), `RETRIEVAL_FUSION` (rrf | weighted) and the BM25 / RRF / weight knobs choose how chunks are
found; `semantic` (embeddings only) is the default. Every key is listed in [`docs/configuration.md`](docs/configuration.md).

## API

All routes except `/health` and `/auth/login|refresh` need a bearer token with the listed permission.

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/login`, `/auth/refresh` | email + password → access/refresh tokens; refresh |
| GET | `/auth/me` | who am I + effective permissions |
| GET | `/traces`, `/traces/{id}`, `/usage` | request/ingest trace log and per-day aggregates (`traces:read`) |
| GET/POST/PATCH/DELETE | `/users`, `/roles` (`GET /permissions`) | admin: manage users, roles and their permissions (`users:manage`, `roles:manage`) |
| POST | `/documents` | multipart upload → `{document, job}` (413 over the size limit, 415 for bad types) |
| GET | `/documents`, `/documents/{id}`, `/documents/{id}/chunks` | list / detail with jobs / indexed chunks |
| PUT | `/documents/{id}` | upload a new version; only changed chunks are re-embedded |
| DELETE | `/documents/{id}` | removes the row, chunks, vectors and the stored file |
| GET | `/jobs/{id}` | `pending → running → done \| failed` (+ error) |
| POST | `/chat` | `{question, document_ids?, top_k?}` → `{answer, sources[]}` |
| POST | `/search` | `{query, top_k?, mode?, fusion?, neighbors?, document_ids?, include_text?}` → ranked `results[]` (no LLM; `search:use`; see [docs/api.md](docs/api.md)) |
| GET | `/health` | db / redis / the active vector store (critical); `vector_settings` (stored index matches the configuration) and ollama (informational) |

## Kubernetes

`k8s/` holds a Kustomize base (api, worker, beat, frontend, migrate Job, Postgres, Redis, Chroma, a
shared uploads volume) and `dev` / `prod` overlays (prod adds replicas, HPAs and an Ingress; dev adds
an in-cluster Ollama component).

```bash
make images REGISTRY=ghcr.io/you/rag-pipeline TAG=v0.1.0   # linux/amd64 builds (PLATFORM=linux/arm64 for local arm64)
make deploy-dry ENV=prod                                      # render only
make deploy ENV=dev                                           # delete+apply migrate job, apply, wait
```

To try it on a laptop, follow [docs/kubernetes-local.md](docs/kubernetes-local.md) (kind, with a
`k8s/overlays/kind` overlay that uses locally built images and the host's Ollama).

Things you must provide: real values in `k8s/base/secrets.yaml` (including a random `SECRET_KEY` of 32+ characters — the API refuses to start in prod with a placeholder — and the bootstrap `ADMIN_*`) (use sealed-secrets/SOPS), image
tags in the overlay, an ingress host/class, and a **ReadWriteMany** storage class for the uploads
volume when running more than one API/worker replica (the API writes files the workers read).

## Quality gates and tests

```bash
make check     # ruff + strict mypy + pytest (backend) · eslint + prettier + tsc + vitest (frontend)
```

Automated tests use fakes for external services; separate real-service smoke tests are run before a
change is considered done (see the next section).

## Verified status

Honest accounting of what has been exercised against real services versus only unit-tested:

| Area | How it was exercised | Result |
|---|---|---|
| Parse (PDF text layer, OCR, vision, DOCX, txt/md) | real OpenRouter, Tesseract, docling Word backend; native PDF text on the real Selling Guide chapters (worker log: 44/44 pages native, 0 via backend) | passed |
| Ingest pipeline (Celery) | real worker, Postgres, Chroma, Ollama embeddings | passed |
| Documents / jobs API | real API + worker over HTTP | passed |
| Chat (retrieval + LLM) | real OpenRouter free model, cited answers | passed |
| Incremental update | real stack: edit 1 paragraph of a 126-chunk doc, `PUT` → **1** chunk re-embedded, 124 reused | passed |
| Incremental update on real revisions | Fannie Mae Selling Guide ch. B3-5, Oct 8 → Nov 5 2025 (real PDFs, real OCR): `PUT` reused **173/342** chunks, embedded 169 (a rebuild embeds 342) | passed (answer check after the update not run: LLM rate-limited) |
| Vector store options (Chroma / pgvector, metric, HNSW / IVFFlat) | one contract-test suite on **both** stores against real Chroma + Postgres/pgvector (metric orderings and score values, filters, incremental-update primitives, `pg_indexes`, `EXPLAIN`), plus the real stack on chroma-l2, chroma-cosine, pgvector hnsw and pgvector ivfflat: 388 real chunks, mismatch detected at startup and fixed with `manage reindex` | passed (retrieval 8/12, hit@1 7/12 on all four: identical because the data is tiny and the embeddings normalised, so this shows nothing about ANN recall; the planner ignores both indexes at 388 rows, forced-index recall@5 vs exact: hnsw 1.00, ivfflat 0.97) |
| Retrieval modes (semantic / keyword BM25 / hybrid RRF + weighted) | unit tests with hand-computed fusion and BM25 values; integration tests on real Postgres + real Chroma **and** pgvector (exact-ID query missed by semantic found by keyword/hybrid, paraphrase missed by keyword found by semantic/hybrid, filters, de-duplication, overrides, drift); real stack (Chroma and pgvector) on 388 real chunks, 12 labelled questions, 3 runs each, 0 LLM calls: hit/hit@1 semantic 8/7, BM25 keyword **10**/7, hybrid RRF 9/7, hybrid weighted 9/6, plain `ts_rank_cd` 5/1 | passed (see `tmp/smoke_tests/23-retrieval-modes-result.md`; **hybrid did not beat keyword-only on this corpus**; the section-ID question is found by keyword (rank 1) but hybrid RRF ranks it 20th; only 12 questions on one corpus, so differences of 1-2 questions are not significant; the dashboard has no mode control yet) |
| Dashboard | real Chromium driving upload → live job → chat → delete | passed |
| Traces + usage dashboard | real stack + real Chromium: real ingest (ok and corrupt-PDF failure), chat (LLM rate-limited → error traces), API/UI numbers cross-checked, charts reviewed in light/dark | passed (token chart with real token data not seen: LLM returned only 429s) |
| Langfuse tracing | traces read back from a real Langfuse project | passed |
| **Docker Compose, all services as containers** | `docker compose up --build`, requests through the containerised frontend | passed (Ollama must run on the host) |
| Backend image size | 5.72 GB → 1.10 GB (torch removed via docling-slim; uv cache kept out of the layer; thin `chromadb-client` instead of the full server package); parsers + stack re-verified | passed (auth smoke 45/47: the 2 LLM-dependent checks hit the free-tier 429) |
| Production frontend image (nginx) | built, served SPA routes and proxied `/api` to a live backend | passed |
| Kubernetes manifests (kind overlay) | applied to a real single-node kind cluster (arm64 images): all pods Ready, migrate Job (Alembic to 0004), login, upload -> worker ingest -> chunks -> retrieval through port-forward, api/worker shared uploads volume, pod-delete survival (api, worker, db, chroma, redis), 2 workers ingesting 6 docs. Found and fixed 2 real bugs and 1 hardening (service-link env crash of Chroma, Chroma volume at the wrong path losing vectors on restart, the first migrate Job run failed with its logs lost, a wait-for-db init container was added as a precaution, race not conclusively reproduced) | passed (chat answer not tested: no real LLM key; in-cluster Ollama component and the `prod` overlay were **not** applied; Ollama was the host's) |
| Kubernetes HPA | metrics-server installed on kind; prod HPAs read live CPU (`4%/70%`) and raised replicas to `minReplicas` | scale-up under load **not measured** (8 GiB Docker VM could not take it); RWX across nodes **not tested** (single node) |
| Auth + RBAC (users, roles, permissions) | real stack through the containerised frontend: 47 checks incl. real Chromium UI flows, forged/expired/wrong-key tokens, live role edits and deactivation | passed (no login rate-limiting; no per-user document ownership; tokens are not revocable before expiry beyond deactivation) |
| Load / scale behaviour (many workers) | 2 workers on one laptop node only; 3 workers starved an 8 GiB Docker VM | not measured beyond that |

Smoke-test scripts and reports are kept locally under `tmp/` (gitignored); the automated suite
(`make check`) runs 250+ backend and 24 frontend tests with fakes for external services.

## Repository map

`backend/app` — `routers/` (HTTP), `services/` (parse, chunk, embed, retrieve, QA, ingest, cleanup),
`tasks/` (Celery), `db/` (SQLAlchemy + Alembic), `schema/` (pydantic) · `frontend/src` — API client,
zustand store, pages · `k8s/` — manifests · `docs/` — architecture and ADRs · `context/` — founding
scope. Operating rules for contributors and agents are in [CLAUDE.md](CLAUDE.md).
