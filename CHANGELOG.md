# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project follows [Semantic Versioning](https://semver.org/); while the version is 0.y.z, minor versions may contain
breaking changes (they will be listed under "Changed" or "Removed").

## [Unreleased]

### Added
- Optional HTTPS: the `prod` and `headless` Ingresses terminate TLS (cert-manager annotation, HTTP-to-HTTPS redirect), and the frontend image serves `:8443` when `TLS_CERT_FILE` / `TLS_KEY_FILE` are set (`docker-compose.tls.yml`, `make up-image-tls`, `make tls-selfsigned`). See `docs/deployment.md`.
- Release automation: `.github/workflows/release.yml` builds multi-arch (amd64 + arm64) backend and frontend images on a
  `vX.Y.Z` tag, scans them with Trivy, publishes them to GHCR with provenance, SBOM and a signed attestation, and creates
  the GitHub Release.
- `docker-compose.image.yml` (`make up-image` / `make down-image`) to run the published images without building, and
  `make release-dry-run` to build both images locally.
- `docs/deployment.md`: source vs published images, tags, upgrading, Kubernetes image overrides, release checklist.

### Changed
- **PDF library: pypdfium2 replaces PyMuPDF.** PyMuPDF is AGPL-3.0, which does not fit an MIT project and its images. Page count,
  native text and page renders (for OCR and vision) now use pypdfium2 (Apache-2.0 / BSD-3). Retrieval results and incremental-update chunk
  reuse on the real test documents are unchanged; the backend image is about 100 MB smaller. A unit test fails if PyMuPDF returns.
  See ADR-0002.
- **Dependency security bumps** (fixes for the Trivy release gate): pillow 12.3.0 to 13.0.0, certifi 2026.7.22 to 2026.10.23,
  urllib3 2.7.0 to 2.8.0, docling-slim 2.126.0 to 2.132.0.
- **Frontend image** is built on `nginxinc/nginx-unprivileged:1.30-alpine` (nginx runs as a non-root user) and now listens on
  container port 8080 instead of 80; `docker-compose.image.yml` maps the host port to 8080 and the Kubernetes Service still
  exposes port 80 (target port 8080). Alpine security updates are applied at build time. If you ran the image with
  `-p X:80`, use `-p X:8080`.
- **Backend image**: the `uv` build image is pinned to an exact version instead of `latest`, and Debian security updates are
  applied at build time.
- `ci.yml` is callable from other workflows and its third-party actions are pinned to commit SHAs.
- `.dockerignore` files also exclude `.env*`, `.git`, `data`, `tmp`, `__pycache__`, `.DS_Store` from build contexts.

## [0.1.0] - Unreleased (first tagged release)

First public version. What is in it, as exercised by the project's own tests and smoke runs (see the status table in the
README for what was verified end to end and what was not):

### Added
- **Ingestion**: upload pdf, docx, txt, md (`POST /v1/documents`) or ingest from an http(s) or `s3://` link; Celery workers
  parse, chunk, embed and index, with pollable job status. Local-disk or S3-compatible file storage.
- **Parsing**: PDF text layer first, OCR (Tesseract) or a vision LLM (OpenRouter, OpenAI, Anthropic) for scanned pages;
  DOCX through docling to markdown. Provider, model, prompt, DPI and timeouts are configuration.
- **Chunking**: content-defined chunking with hashed chunks, so a document edit re-embeds only the changed chunks.
- **Retrieval**: Chroma (HNSW; l2/cosine/ip) or pgvector (HNSW/IVFFlat); semantic, keyword (BM25) and hybrid (RRF or weighted)
  search; `POST /v1/search` without an LLM.
- **Answers**: `POST /v1/chat` and `/v1/chat/stream` (Server-Sent Events) with cited sources; no LLM call when nothing relevant
  is retrieved. Redis query cache with invalidation on document change, and a graceful fallback when Redis is down.
- **Access control**: JWT login, admin-defined roles and permissions, API keys for services.
- **Admin Settings**: runtime-editable defaults with write-only encrypted provider secrets and an audit log
  (environment variables still win).
- **Dashboard** (React + Vite): documents and jobs, chat with citations and markdown, traces, usage and cost, cache, settings.
- **Observability**: per-request traces and usage in the dashboard, optional Langfuse spans, structured JSON logs.
- **Deployment**: Docker Compose stack (db, redis, chroma, migrate, api, worker, beat, frontend) and Kustomize overlays
  (base, dev, kind, prod with HPA and ingress, headless) verified on kind.
- Versioned `/v1` API with a committed OpenAPI contract and a breaking-change guard; unversioned paths remain as deprecated aliases.
- Query-cache benchmark with method and caveats in [docs/performance.md](docs/performance.md) (measured on one laptop).

### Known limitations
- Kubernetes was verified on kind only; in-cluster Ollama and a real RWX storage class were not exercised.
- Worker throughput under many workers is not measured; the benchmark numbers come from one machine.
- No login rate limiting; no per-user document ownership; access tokens cannot be revoked before they expire.
- The frontend image's nginx master process runs as root.

[Unreleased]: https://github.com/aayushjoshi-12/rag-pipeline/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/aayushjoshi-12/rag-pipeline/releases/tag/v0.1.0
