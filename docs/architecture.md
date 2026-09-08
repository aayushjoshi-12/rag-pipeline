# Architecture

System diagram for `rag-pipeline`. Two primary user-facing flows — **ingest** (upload →
indexed) and **query** (question → answer + sources) — plus scheduled cleanup, all behind one
FastAPI backend with horizontally-scaling async workers.

```mermaid
flowchart LR
    subgraph CLIENT["Client layer"]
        UI["Dashboard UI<br/>React + Vite :5173"]
        USER["API consumers<br/>curl / SDK / scripts"]
    end

    subgraph API["Backend — FastAPI :8000 (backend/app)"]
        RDOC["POST /documents<br/>accept upload → create Job → enqueue"]
        RCHAT["POST /chat<br/>RAG query"]
        RHEALTH["GET /health"]
        SVC_QA["Retrieval / QA service<br/>retrieve → synthesize"]
        JOBDB["Job status read"]
    end

    subgraph ASYNC["Async — Celery + Redis (owns parse · chunk · embed · cleanup)"]
        BROKER["Redis<br/>broker + result backend"]
        WORKER["Celery worker<br/>ingest / embed / cleanup"]
        BEAT["Celery beat<br/>periodic cleanup"]
        PARSER["Parser service<br/>backend/app/services/parser.py"]
    end

    subgraph DATA["Data layer"]
        PG[("Postgres + pgvector<br/>documents · chunks · jobs<br/>traces — metadata + vectors")]
        CHROMA[("ChromaDB<br/>document vector store")]
    end

    subgraph PROVIDERS["External providers — pluggable"]
        EMB["Embedding API<br/>Ollama mxbai-embed-large"]
        LLM["Answer synthesis<br/>OpenRouter (free tier)<br/>nemotron-3-super-120b"]
        VIS["Vision parsing<br/>OpenRouter (free tier)<br/>nemotron-3-nano-omni"]
    end

    OBS["Langfuse<br/>optional self-host<br/>traces / usage"]

    UI --> RDOC
    UI --> RCHAT
    USER --> RDOC
    USER --> RCHAT
    RHEALTH --> PG

    %% —— Ingest path（1）：API only accepts, the worker does the work ——
    RDOC -->|create Job| PG
    RDOC -->|enqueue ingest task| BROKER
    BROKER --> WORKER
    BEAT --> WORKER
    WORKER -->|1 parse| PARSER
    PARSER -->|raw text| WORKER
    WORKER -->|2 chunk + 3 embed| EMB
    WORKER -->|4 store chunks| CHROMA
    WORKER -->|5 update job / metadata| PG
    UI -->|poll status| JOBDB
    JOBDB --> PG

    %% —— Query path（2） ——
    RCHAT --> SVC_QA
    SVC_QA -->|similarity + metadata filter| CHROMA
    SVC_QA -->|sources + context| PG
    SVC_QA -->|answer synthesis| LLM
    SVC_QA --> OBS

    UI -.->|traces / usage / ingestion status| OBS
```

## Deployment matrix

| Concern | Local dev (Compose) | Prod (Kubernetes) |
|---|---|---|
| API | 1 container `:8000` | Deployment + ClusterIP svc + HPA (2–8) |
| Workers | 1 container | Deployment + HPA (2–8), same image |
| Postgres+pgvector | compose volume | PVC-backed Deployment |
| Redis | compose | Deployment + service |
| ChromaDB | compose (**embedded** mode = zero-dep dev) | Deployment + PVC (**server** mode) |
| Dashboard | Vite dev `:5173` | Serve built SPA (future: ingress) |
| Secrets | `.env` (dev) | K8s `Secret` (prod) |

## Design boundaries

- **The API never parses, chunks, embeds, or cleans up.** Its only job on ingest is to accept
  the upload, record the Job, and enqueue a Celery task. All document processing lives in the
  async worker path. A dashboard surface to inspect/manage parsing output may come later — it
  reads state; it doesn't get a parse/embed API.
- The same backend image runs the API **and** the worker — the difference is only the container
  command (`uvicorn` vs `celery -A app.celery_app worker`).
- Parsing is original code in `backend/app/services/` (seam `app/services/parser.py`), so the
  pipeline is self-contained.
- ChromaDB is embedded in dev (zero external deps) and switchable to server mode for prod — see
  OPEN decision D1 in `context/v0/README.md`.
- Scaling is the headline capability: API and workers scale horizontally behind Redis-backed
  Celery tasks; no shared-state coupling beyond the datastores.