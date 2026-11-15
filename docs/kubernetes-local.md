# Running the Kubernetes manifests on a local kind cluster

This is the flow used to verify `k8s/` on real kind clusters (kind v0.33, Kubernetes 1.37, Docker
Desktop on Apple Silicon; last full run 2026-11-15 on the code at Alembic head 0010). Needs `docker`, `kubectl`, `kind` (`brew install kind`) and an Ollama on the
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
  --from-literal=SETTINGS_ENCRYPTION_KEY="$(openssl rand -base64 48)" \   # enables write-only secrets in the Settings window
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

- **Start-up order.** `kubectl apply -k` starts Postgres, the migrate Job and the api together. The api seeds the
  built-in roles and the bootstrap admin once at start-up, so it now has a `wait-for-migrate` init container (holds the
  pod until `alembic current` reports head). Without it, an api that won the race was Ready but nobody could log in
  (found on the 2026-11-15 run).
- **Resources.** The full stack requests about 2.5 CPU / 2.2 GiB. A worker now idles at ~445 MiB (request 512Mi,
  limit 3Gi): `WORKER_CONCURRENCY` defaults to 2 (Celery's own default is one child per *node* CPU, which put the
  idle worker at 1.1-1.3 GiB, above its old 1 Gi request). On an 8 GiB Docker VM shared with other containers, scaling
  workers beyond 2 starved the kind control plane (API-server `TLS handshake timeout`, probe-driven restarts); stop other
  containers or give Docker 12+ GiB. Chroma sizing: `150 MiB + 16 bytes x dimension x vectors` (512Mi/1Gi = ~40k vectors
  of 1024 dims); see `docs/configuration.md` "Memory and limits" for the measurements and the Chroma delete leak.
  The old kernel-log OOM kills of Chroma (5.7 GB) and uvicorn (3.3 GB) were global VM kills, which kubelet reports as
  `OOMKilled` for the one unexplained api pod of the first run (ticket 48).
- **Probes.** The api has a `startupProbe` (up to 5 min) and 5 s timeouts on every probe: `/health` pings
  db/redis/chroma/ollama and the 1 s default produced `context deadline exceeded` while the node pulled images.
  Not yet re-verified on a throttled kind node (ticket 49).
- **Uploads volume, single node.** kind's default StorageClass (`rancher.io/local-path`) is ReadWriteOnce and
  node-local; api and worker share the files because they are on the one node.
- **Uploads volume, several nodes (measured on 2 worker nodes).** The local-path PV carries a node affinity, so the
  scheduler puts **every** api and worker pod on that one node (no spreading), and a worker forced onto the other
  node stays `Pending` (`didn't match PersistentVolume's node affinity`). It does not "fail across nodes", it
  silently refuses to spread. Two real answers: an RWX StorageClass (NFS, EFS, Filestore, CephFS) on the PVC, or
  `STORAGE_BACKEND=s3` with `k8s/components/s3` (drops the PVC, `/app/data` becomes an `emptyDir`). The S3 route was
  verified on kind with an in-cluster Floci: api on node A uploaded, worker on node B ingested, search worked.
  ```yaml
  # overlay kustomization.yaml
  components: [../../components/s3]
  patches:
    - target: {kind: ConfigMap, name: rag-pipeline-config}
      patch: |-
        - {op: replace, path: /data/S3_ENDPOINT_URL, value: "http://floci:4566"}   # empty = real AWS
  # then once: kubectl exec deploy/rag-pipeline-api -- python -m app.manage ensure-bucket
  ```
  Put `S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY` in the Secret (or use the pod's IAM role). Floci itself is not shipped.
- **HPA.** Needs metrics-server, which kind does not ship:
  `kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml`, then add
  `--kubelet-insecure-tls` to its args (kind kubelets use self-signed certs). Measured on 2026-11-15 with the prod
  `hpa.yaml`: a 16-thread in-cluster loop of `POST /v1/search` (hybrid, `cache:false`) for 240 s scaled the api from 2
  to 7 within 60 s and to the cap of 8, with 0 errors in 13,254 requests; after the load stopped it fell back to 2 in
  about 3m40s (default downscale stabilisation).
- **Ingress.** The prod and headless Ingress manifests do not set `ingressClassName`. On kind with ingress-nginx
  (`kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.1/deploy/static/provider/kind/deploy.yaml`,
  cluster created with `extraPortMappings` 80/443 and the `ingress-ready=true` node label) the `nginx` class is **not**
  the default, so an Ingress without a class is silently ignored: add `ingressClassName: nginx` in your overlay.
  Then `curl -H 'Host: rag.example.com' http://localhost:<mapped port>/`. SSE (`/chat/stream`) is not buffered through
  ingress-nginx -> the frontend's nginx -> the API (the API sends `X-Accel-Buffering: no`; first token 1.3 s, done 2.3 s).
- **Settings across replicas.** A change saved in the Settings window is visible on every api replica within
  `SETTINGS_REFRESH_SECONDS` (5 s); measured with 2 replicas: the writer instantly, the other replica after 2-3 s.
- **Secrets.** Never put real values in `k8s/base/secrets.yaml`; create the Secret out of band as above
  (or use sealed-secrets/SOPS in a real cluster). `SETTINGS_ENCRYPTION_KEY` must be in it for write-only secrets.
- **Not verified on kind:** the in-cluster Ollama component (`dev` overlay); the 3-replica api / 3-replica worker numbers of
  the untouched `prod` overlay (it was applied trimmed to 2 api / 1 worker to fit an 8 GiB VM); a real RWX class.
