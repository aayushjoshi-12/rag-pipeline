# Running the Kubernetes manifests on a local kind cluster

This is the exact flow used to verify `k8s/` on a real cluster (kind v0.33, Kubernetes 1.37, Docker
Desktop on Apple Silicon). Needs `docker`, `kubectl`, `kind` (`brew install kind`) and an Ollama on the
host with `mxbai-embed-large` pulled (`ollama pull mxbai-embed-large`).

The `kind` overlay (`k8s/overlays/kind`) differs from `dev`: locally built `:kind` images, **no
in-cluster Ollama** (embeddings use the host's Ollama via `host.docker.internal`, which avoids a
multi-GB model pull inside the cluster), a ReadWriteOnce uploads volume, and it **omits the placeholder
Secret** so applying it never overwrites the real one.

```bash
# 1. cluster + images (native architecture: arm64 on Apple Silicon; the default PLATFORM stays amd64)
kind create cluster --name rag-kind
make images PLATFORM=linux/arm64 TAG=kind      # use linux/amd64 on an x86 host
kind load docker-image rag-pipeline/backend:kind rag-pipeline/frontend:kind --name rag-kind

# 2. secrets: real random values, kept outside git
kubectl apply -f k8s/base/namespace.yaml
kubectl create secret generic rag-pipeline-secrets -n rag-pipeline \
  --from-literal=SECRET_KEY="$(openssl rand -base64 48)" \
  --from-literal=POSTGRES_PASSWORD="$PGPW" \
  --from-literal=DATABASE_URL="postgresql+asyncpg://rag:$PGPW@db:5432/rag" \
  --from-literal=ADMIN_EMAIL=admin@example.com --from-literal=ADMIN_PASSWORD="$(openssl rand -hex 12)" \
  --from-literal=OPENROUTER_API_KEY=replace-me     # a real key is only needed for /chat

# 3. deploy (Jobs are immutable: delete the old migrate Job first, as `make deploy` does)
kubectl delete job rag-pipeline-migrate -n rag-pipeline --ignore-not-found
kubectl apply -k k8s/overlays/kind
kubectl wait --for=condition=complete job/rag-pipeline-migrate -n rag-pipeline --timeout=300s
kubectl -n rag-pipeline get pods

# 4. use it (ports chosen not to clash with a Compose stack on 8000/5173)
kubectl -n rag-pipeline port-forward svc/rag-pipeline-api 18000:80 &
kubectl -n rag-pipeline port-forward svc/rag-pipeline-frontend 15173:80 &
curl localhost:18000/health

# 5. teardown
kind delete cluster --name rag-kind
```

## Things to know

- **Resources.** The full stack requests about 3 CPU / 3 GiB and each worker is allowed 3 GiB. On an 8 GiB
  Docker VM that is shared with other containers, scaling workers beyond 2 starved the kind control
  plane (API-server `TLS handshake timeout`, probe-driven restarts). Give Docker 12+ GiB for scale tests.
- **Uploads volume (RWX).** kind's default StorageClass (`rancher.io/local-path`) is ReadWriteOnce and
  node-local. A single-node kind cluster therefore proves that api and worker pods share the volume
  *contents* (verified), but not that RWX works across nodes. On a multi-node cluster api and worker
  pods on different nodes cannot mount an RWO volume; use an RWX class (NFS, EFS, Filestore, CephFS).
- **HPA.** The prod HPAs need metrics-server, which kind does not ship. See the README "Verified
  status" table for whether it was measured.
- **Secrets.** Never put real values in `k8s/base/secrets.yaml`; create the Secret out of band as above
  (or use sealed-secrets/SOPS in a real cluster).
