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
- **Observe** — Langfuse spans for every ingest stage and chat request; prompt/question content is
  only sent when `LANGFUSE_CAPTURE_CONTENT=true`. Structured JSON logs carry `job_id` / `chat_id`.
- **Dashboard** — upload with live job status, document list/detail with chunks, chat with citations.
- **Access control** — JWT login with role-based access: admins define roles as sets of permissions
  (`documents:read|write|delete`, `jobs:read`, `chat:use`, `users:manage`, `roles:manage`) and assign
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

## API

All routes except `/health` and `/auth/login|refresh` need a bearer token with the listed permission.

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/login`, `/auth/refresh` | email + password → access/refresh tokens; refresh |
| GET | `/auth/me` | who am I + effective permissions |
| GET/POST/PATCH/DELETE | `/users`, `/roles` (`GET /permissions`) | admin: manage users, roles and their permissions (`users:manage`, `roles:manage`) |
| POST | `/documents` | multipart upload → `{document, job}` (413 over the size limit, 415 for bad types) |
| GET | `/documents`, `/documents/{id}`, `/documents/{id}/chunks` | list / detail with jobs / indexed chunks |
| PUT | `/documents/{id}` | upload a new version; only changed chunks are re-embedded |
| DELETE | `/documents/{id}` | removes the row, chunks, vectors and the stored file |
| GET | `/jobs/{id}` | `pending → running → done \| failed` (+ error) |
| POST | `/chat` | `{question, document_ids?, top_k?}` → `{answer, sources[]}` |
| GET | `/health` | db / redis / chroma (critical) and ollama (informational) |

## Kubernetes

`k8s/` holds a Kustomize base (api, worker, beat, frontend, migrate Job, Postgres, Redis, Chroma, a
shared uploads volume) and `dev` / `prod` overlays (prod adds replicas, HPAs and an Ingress; dev adds
an in-cluster Ollama component).

```bash
make images REGISTRY=ghcr.io/you/rag-pipeline TAG=v0.1.0   # linux/amd64 builds
make deploy-dry ENV=prod                                      # render only
make deploy ENV=dev                                           # delete+apply migrate job, apply, wait
```

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
| Dashboard | real Chromium driving upload → live job → chat → delete | passed |
| Langfuse tracing | traces read back from a real Langfuse project | passed |
| **Docker Compose, all services as containers** | `docker compose up --build`, requests through the containerised frontend | passed (Ollama must run on the host) |
| Backend image size | 5.72 GB → 1.42 GB (torch removed via docling-slim; uv cache kept out of the layer); parsers + stack re-verified on the slim image | passed (auth smoke 45/47: the 2 LLM-dependent checks hit the free-tier 429) |
| Production frontend image (nginx) | built, served SPA routes and proxied `/api` to a live backend | passed |
| Kubernetes manifests | rendered with `kubectl kustomize` and checked structurally by tests (references, selectors, ports, config keys) | **not applied to a cluster** — untested at runtime |
| Auth + RBAC (users, roles, permissions) | real stack through the containerised frontend: 47 checks incl. real Chromium UI flows, forged/expired/wrong-key tokens, live role edits and deactivation | passed (no login rate-limiting; no per-user document ownership; tokens are not revocable before expiry beyond deactivation) |
| Load / scale behaviour (HPA, many workers) | not measured | — |

Smoke-test scripts and reports are kept locally under `tmp/` (gitignored); the automated suite
(`make check`) runs 250+ backend and 24 frontend tests with fakes for external services.

## Repository map

`backend/app` — `routers/` (HTTP), `services/` (parse, chunk, embed, retrieve, QA, ingest, cleanup),
`tasks/` (Celery), `db/` (SQLAlchemy + Alembic), `schema/` (pydantic) · `frontend/src` — API client,
zustand store, pages · `k8s/` — manifests · `docs/` — architecture and ADRs · `context/` — founding
scope. Operating rules for contributors and agents are in [CLAUDE.md](CLAUDE.md).
