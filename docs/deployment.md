# Deployment: from source or from the published images

Two ways to run the stack, plus Kubernetes. All of them use the same code and the same settings (see
[configuration.md](configuration.md)).

| Path | When | Command |
|---|---|---|
| From source | developing, or you want to change the code | `make up` (builds the images from this checkout) |
| Published images | trying it out, self-hosting, no build toolchain | `docker compose -f docker-compose.image.yml up -d` (or `make up-image`) |
| Kubernetes | a cluster | Kustomize overlays in `k8s/`, with the published images (below) |

## Published images

Two images are published to the GitHub Container Registry by `.github/workflows/release.yml` when a version tag is pushed:

| Image | Contents |
|---|---|
| `ghcr.io/aayushjoshi-12/rag-pipeline-backend` | API, Celery worker, Celery beat and the Alembic migration: one image, the command differs (`uvicorn`, `celery ... worker`, `celery ... beat`, `alembic upgrade head`). Runs as UID 10001. |
| `ghcr.io/aayushjoshi-12/rag-pipeline-frontend` | The dashboard: static build served by nginx, `/api/` proxied to `API_UPSTREAM`. Built on `nginxinc/nginx-unprivileged`: nginx runs as a non-root user and listens on container port 8080 (the Kubernetes Service still exposes port 80). |

Platforms: `linux/amd64` and `linux/arm64` (one multi-arch tag; Docker pulls the right one).

### Tags and versioning

Versions follow [Semantic Versioning](https://semver.org). Tag `vX.Y.Z` on `main` produces:

- `X.Y.Z` (immutable in practice: never re-pushed),
- `X.Y` (moves to the newest patch),
- `latest` (newest stable release).

Pre-releases (`v0.2.0-rc.1`) get only their exact tag and a GitHub "pre-release"; they never move `X.Y` or `latest`.
While the version is `0.y.z` the API and settings may still change between minor versions; breaking changes are listed
in [CHANGELOG.md](../CHANGELOG.md). Pin `X.Y.Z` for anything you care about; `latest` is for trying it out.

## Run the published images

```bash
cp .env.example .env     # set SECRET_KEY, SETTINGS_ENCRYPTION_KEY, ADMIN_EMAIL, ADMIN_PASSWORD (see "Required secrets")
RAG_VERSION=0.1.0 docker compose -f docker-compose.image.yml up -d
```

- Dashboard http://localhost:5173, API http://localhost:8000/docs.
- `docker-compose.image.yml` has the same services, healthchecks, memory limits and volumes as `docker-compose.yml`
  (db, redis, chroma, migrate, api, worker, beat, frontend), without the `build:` sections, so it needs no source
  tree beyond itself, `.env` and `chroma/config.yaml` (mounted into Chroma). Use one stack or the other: they share
  host ports and project names.
- Embeddings default to Ollama on the host (`ollama pull mxbai-embed-large`); chat and vision parsing need a provider
  key, set either in `.env` or later in Admin > Settings.
- Knobs: `RAG_VERSION` (default `latest`), `RAG_IMAGE_REGISTRY` (default `ghcr.io/aayushjoshi-12`, e.g. to use a
  mirror or a fork), `RAG_ENV_FILE`, and host ports `RAG_API_PORT`, `RAG_UI_PORT`, `RAG_DB_PORT`, `RAG_REDIS_PORT`,
  `RAG_CHROMA_PORT`.
- If you keep the settings in a file other than `.env`, set `RAG_ENV_FILE` **and** pass the same file to Compose for variable
  substitution (`docker compose --env-file my.env -f docker-compose.image.yml up -d`); otherwise the admin account in
  `ADMIN_EMAIL` / `ADMIN_PASSWORD` is substituted as empty and nobody can sign in. With the default `.env` this is automatic.
- After recreating only the API container, restart the frontend too (`docker compose ... restart frontend`): nginx keeps the
  API's old address until it restarts.
- Stop with `make down-image` (volumes are kept; add `-v` to `docker compose ... down` only to delete your data).

## Required secrets

| Variable | What for | How to make one |
|---|---|---|
| `SECRET_KEY` | signs login tokens; the API refuses to start in prod with a placeholder | `openssl rand -base64 48` |
| `SETTINGS_ENCRYPTION_KEY` | encrypts provider keys saved from Admin > Settings; empty disables write-only secret fields | `openssl rand -base64 48` |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | first admin, created only while there are no users | choose a strong password |
| `OPENROUTER_API_KEY` (or OpenAI/Anthropic) | chat and vision parsing; optional for search-only use | from your provider |

Single-quote values in `.env` (bash mangles `&`, `;`, `!`, `$`). A key present in `.env` is pinned by the environment
and shown locked in Admin > Settings; leave out what you want to manage there. Rotating `SETTINGS_ENCRYPTION_KEY`
makes already-stored secrets unreadable, so re-enter them afterwards.

## Upgrading

1. Read the [CHANGELOG](../CHANGELOG.md) for the target version.
2. **Back up first.** Postgres: `docker compose -f docker-compose.image.yml exec db pg_dump -U rag rag > rag-backup.sql`.
   Chroma: stop `chroma`, copy the `chroma-data` volume (for example
   `docker run --rm -v <project>_chroma-data:/data -v "$PWD":/backup alpine tar czf /backup/chroma-data.tgz -C /data .`),
   and the `appdata` volume likewise (uploads). Postgres alone is not enough to restore vectors, but `manage reindex`
   can rebuild the vector store from the stored chunks.
3. `RAG_VERSION=<new> docker compose -f docker-compose.image.yml pull && RAG_VERSION=<new> docker compose -f docker-compose.image.yml up -d`.
   The `migrate` service runs `alembic upgrade head` before the api, worker and beat start; it is idempotent, so a
   re-run is harmless. Migrations only move forward: to go back, restore the backup and start the older version.
4. Check `GET /health`.

## Kubernetes overview

`k8s/` holds a Kustomize base (api, worker, beat, frontend, migrate Job, Postgres, Redis, Chroma, a
shared uploads volume) and `dev` / `prod` overlays (prod adds replicas, HPAs and an Ingress; dev adds
an in-cluster Ollama component).

```bash
make images REGISTRY=ghcr.io/you/rag-pipeline TAG=v0.1.0   # linux/amd64 builds (PLATFORM=linux/arm64 for local arm64)
make deploy-dry ENV=prod                                      # render only
make deploy ENV=dev                                           # delete+apply migrate job, apply, wait
```

`headless` serves only the API (no dashboard) behind an Ingress, and `k8s/components/s3` keeps uploads in S3
instead of a shared volume (the multi-node answer). To try it on a laptop, follow
[kubernetes-local.md](kubernetes-local.md) (kind, with a `k8s/overlays/kind` overlay that uses locally
built images and the host's Ollama; the Ingress needs your cluster's `ingressClassName`).

Things you must provide: real values in `k8s/base/secrets.yaml` (including a random `SECRET_KEY` of 32+ characters — the API refuses to start in prod with a placeholder — and the bootstrap `ADMIN_*`) (use sealed-secrets/SOPS), image
tags in the overlay, an ingress host/class, and, when running more than one API/worker replica, either a **ReadWriteMany** storage class for the uploads
volume (the API writes files the workers read) or `STORAGE_BACKEND=s3` via `k8s/components/s3`. A ReadWriteOnce volume
does not fail across nodes, it pins every api/worker pod to the volume's node.

## Kubernetes with the published images

The manifests reference the placeholder images `rag-pipeline/backend` and `rag-pipeline/frontend`. Point them at the
published ones with Kustomize image overrides, in an overlay of your own or on an existing one:

```bash
cd k8s/overlays/prod
kustomize edit set image \
  rag-pipeline/backend=ghcr.io/aayushjoshi-12/rag-pipeline-backend:0.1.0 \
  rag-pipeline/frontend=ghcr.io/aayushjoshi-12/rag-pipeline-frontend:0.1.0
kubectl kustomize . | less     # review, then: make deploy ENV=prod
```

(Or edit the `images:` block of the overlay by hand; pin a tag or, better, a digest.) Create the Secret
`rag-pipeline-secrets` with real values first (`SECRET_KEY`, `SETTINGS_ENCRYPTION_KEY`, `POSTGRES_PASSWORD`,
`DATABASE_URL`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, provider keys); [kubernetes-local.md](kubernetes-local.md) shows the
`kubectl create secret` command and the kind walkthrough. The published images are public, so no `imagePullSecret` is
needed unless you mirror them to a private registry. Kubernetes support has been verified on kind only
(see the README status table).

## Verifying an image

Release builds carry a build-provenance attestation (signed with GitHub's Sigstore identity) and an SBOM attached to
the image manifest. After a release exists:

```bash
gh attestation verify oci://ghcr.io/aayushjoshi-12/rag-pipeline-backend:0.1.0 --repo aayushjoshi-12/rag-pipeline
docker buildx imagetools inspect ghcr.io/aayushjoshi-12/rag-pipeline-backend:0.1.0 --format '{{ json .SBOM }}'
```

The release workflow also runs Trivy on each image before pushing it and refuses to publish when a CRITICAL
vulnerability with an available fix is present. It scans the amd64 image only.

## Maintainer release checklist

1. `make check` is green on `main`; CI is green.
2. Bump the version everywhere it lives: `backend/app/version.py` (the single source the API reports), `backend/pyproject.toml`
   (then `uv lock`) and `frontend/package.json` (then `npm install --package-lock-only`).
3. Move `CHANGELOG.md` "Unreleased" entries under a new `## [X.Y.Z] - date` heading and fix the compare links.
4. Commit, then tag and push: `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin main vX.Y.Z`.
5. Watch the **Release** workflow (checks, Trivy, multi-arch build, push, attest, GitHub Release). A manual run of the
   workflow (Actions > Release > Run workflow) does the same without pushing: use it to dry-run changes to the workflow.
6. Make the packages public once (GitHub > your profile > Packages > each image > Package settings > visibility), link
   them to the repository, and confirm an anonymous `docker pull` works.
7. Smoke the published images on a clean machine: `RAG_VERSION=X.Y.Z docker compose -f docker-compose.image.yml up -d`,
   log in, upload a small document, search it.
