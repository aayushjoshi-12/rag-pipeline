# HTTP API reference

This is the contract for teams that build on the service. The machine-readable version is committed as
[`openapi.json`](./openapi.json) (also served live at `/openapi.json`, Swagger UI at `/docs`); a CI test fails when
a change would break existing clients (see [Compatibility](#compatibility-and-the-contract-test)).

Sections: [Versioning](#versioning) - [Authentication](#authentication) - [Errors and request ids](#errors-and-request-ids) -
[Pagination](#pagination) - [Capabilities](#get-v1capabilities) - [CORS](#cors-browser-clients) -
[End-to-end examples](#end-to-end-examples) - [Headless deployment](#headless-deployment-api-only) - then the per-endpoint
reference (search, API keys, cache).

## Versioning

The supported API lives under **`/v1`**. Every route is also reachable **without the prefix** (`/documents`,
`/search`, ...) as a **deprecated alias** for this release: same behaviour, plus the response headers
`Deprecation: true` and `Link: </v1/documents>; rel="successor-version"`. The dashboard still uses the aliases.
Build new clients on `/v1`; the aliases will be removed in a later release (announced in the changelog). `/health` is
infrastructure and stays unversioned. The OpenAPI document describes `/v1` only.

Two things differ between the alias and `/v1` on purpose:

| | `/v1` | unversioned alias (deprecated) |
|---|---|---|
| `GET /documents` | `{items, total, limit, offset, has_more}`; params `limit` (1..500, default 50), `offset` | bare array; params `skip`, `limit` (clamped) |
| error bodies | `{detail, code, request_id}` | the same (an additive change on both) |

## Authentication

All `/v1` routes except `POST /v1/auth/login` and `POST /v1/auth/refresh` need credentials when
`AUTH_ENABLED=true` (the default in Compose): `Authorization: Bearer <access token>` from `POST /v1/auth/login`
(30-minute JWT, for people and the dashboard), or an **API key** in `X-API-Key` (long-lived, revocable, for services; see
[Authentication for services](#authentication-for-services-api-keys)). A missing or invalid credential is `401`; a valid one
without the needed permission is `403`.

## Errors and request ids

Every error response has the same JSON body:

```json
{"detail": "Document not found.", "code": "not_found", "request_id": "3f0c9c3e6c534d4e9f550b0f7a1d2a10"}
```

- `detail` - human text. For `422` it is the list of validation problems (`[{"loc": [...], "msg": ..., "type": ...}]`).
- `code` - **stable and machine-readable**, derived from the HTTP status; branch on this, not on the text:
  `bad_request` 400, `unauthorized` 401, `forbidden` 403, `not_found` 404, `method_not_allowed` 405, `conflict` 409,
  `payload_too_large` 413, `unsupported_media_type` 415, `validation_error` 422, `rate_limited` 429, `internal_error` 500,
  `bad_gateway` 502, `service_unavailable` 503 (broker, retrieval or vector index unavailable), `gateway_timeout` 504.
- `request_id` - the same value as the `X-Request-ID` response header.

**`X-Request-ID`** is on every response. Send your own (`[A-Za-z0-9._-]`, up to 128 characters) to correlate with your logs;
anything else is replaced by a generated id. The server logs one line per request
(`request_id=... method=... path=... status=... duration_ms=...`) and binds the id to its structured logs, so quoting it is
enough to find the request. Unexpected errors are `500 {"detail": "Internal server error.", "code": "internal_error", ...}`;
internals are never returned.

## Pagination

`GET /v1/documents?limit=50&offset=0` returns

```json
{"items": [{"id": "...", "filename": "...", "status": "ready"}], "total": 137, "limit": 50, "offset": 0, "has_more": true}
```

Newest first, stable across pages (ties broken by id). `limit` is 1..500 (default 50) and `offset` >= 0; values outside the range
are a `422`, never silently clamped. Fetch the next page with `offset += limit` until `has_more` is `false`. `GET /v1/traces`
uses the same `limit`/`offset` and returns `{items, total}`. Other lists (users, roles, API keys, a document's chunks and jobs)
are small and bounded by design and are returned whole.

## `GET /v1/capabilities`

Any authenticated caller (no particular permission). Returns switches and limits, never secrets, hostnames or credentials:

```json
{"version": "0.1.0", "api_versions": ["v1"], "auth_enabled": true,
 "retrieval": {"modes": ["semantic", "keyword", "hybrid"], "default_mode": "semantic", "fusions": ["rrf", "weighted"],
               "default_fusion": "rrf", "keyword_methods": ["bm25", "postgres_fts"], "default_keyword_method": "bm25",
               "default_top_k": 5, "max_top_k": 50, "max_neighbors": 5},
 "vector_store": {"store": "chroma", "metric": "l2", "index": "hnsw"},
 "embedding": {"provider": "ollama", "model": "mxbai-embed-large"},
 "uploads": {"max_bytes": 26214400, "extensions": ["docx", "md", "pdf", "txt"], "content_types": {"pdf": "application/pdf"}},
 "pagination": {"default_limit": 50, "max_limit": 500},
 "features": {"url_ingest": true, "url_ingest_schemes": ["http", "https", "s3"], "storage_backend": "local",
              "query_cache": true, "api_keys": true, "cors_enabled": false}}
```

Use it to adapt a client instead of hard-coding (hide `hybrid` if absent, enforce `uploads.max_bytes` before sending).

## CORS (browser clients)

Off by default: no `Access-Control-*` header is ever sent, so a browser on another origin is blocked while server-side
clients are unaffected. To let a browser app call the API directly, set `CORS_ALLOW_ORIGINS` (comma separated origins such as
`https://app.example.com,https://admin.example.com`, or `*`; see [configuration](./configuration.md#cors)). Allowed: methods
`GET POST PUT PATCH DELETE OPTIONS`; request headers `Authorization`, `Content-Type`, `X-API-Key`, `X-Request-ID`,
`Cache-Control`; exposed response headers `X-Request-ID`, `X-Cache`, `X-Cache-Layers`, `Deprecation`, `Link`, `WWW-Authenticate`.
A preflight (`OPTIONS`) is answered before authentication. **Credentials/cookies are never allowed**
(`Access-Control-Allow-Credentials` is not sent; the API authenticates with headers), so `*` is safe from that angle, but
prefer an explicit list: a bearer token or API key works from any origin you allow. Never put an API key in a public web page;
use a backend-for-frontend, or a narrow `search:use`-only key behind your own gateway.

## End-to-end examples

`scripts/verify_api_docs.py` executes the blocks below (each is preceded by a `verify:` marker) against a running stack, so
they cannot silently rot: `uv run python scripts/verify_api_docs.py --api http://localhost:8000 --email you@example.com`
(password in `$API_PASSWORD`). The blocks use the variables `API` (base URL), `EMAIL` and `PASSWORD`.

### curl: log in, upload, poll the job, search, delete

<!-- verify:curl -->
```bash
set -euo pipefail
API=${API:-http://localhost:8000}
H='content-type: application/json'

# 1. log in (access token for 30 minutes)
TOKEN=$(curl -sf "$API/v1/auth/login" -H "$H" -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# 2. what does this server support?
curl -sf "$API/v1/capabilities" -H "$AUTH" | jq '{version, modes: .retrieval.modes, max_bytes: .uploads.max_bytes}'

# 3. upload a document -> 201 {document, job}
printf '# Pet policy\n\nDogs under 20 kg are allowed in the lobby. Cats are allowed everywhere.\n' > /tmp/api-docs-example.md
UP=$(curl -sf "$API/v1/documents" -H "$AUTH" -F 'file=@/tmp/api-docs-example.md;type=text/markdown')
DOC=$(echo "$UP" | jq -r .document.id); JOB=$(echo "$UP" | jq -r .job.id)
# (or ingest a link: curl "$API/v1/documents/from-url" -H "$AUTH" -H "$H" -d '{"url":"https://www.rfc-editor.org/rfc/rfc8259.txt"}')

# 4. poll the job: pending -> running -> done | failed
STATUS=pending
for i in $(seq 1 60); do
  STATUS=$(curl -sf "$API/v1/jobs/$JOB" -H "$AUTH" | jq -r .status)
  case "$STATUS" in done) break;; failed) echo "ingest failed" >&2; exit 1;; esac
  sleep 2
done
[ "$STATUS" = done ]

# 5. search (no LLM involved)
curl -sf "$API/v1/search" -H "$AUTH" -H "$H" -d "{\"query\":\"are dogs allowed in the lobby\",\"top_k\":3,\"document_ids\":[\"$DOC\"]}" \
  | jq '.results[] | {rank, filename, score_kind, text: .text[:60]}'

# 6. list documents (paginated envelope)
curl -sf "$API/v1/documents?limit=5" -H "$AUTH" | jq '{total, has_more, first: .items[0].filename}'

# 7. delete it (204)
curl -sf -X DELETE "$API/v1/documents/$DOC" -H "$AUTH" -o /dev/null -w 'deleted: %{http_code}\n'
```

Chat calls the configured LLM and costs tokens, so it is a separate block:

<!-- verify:curl-chat -->
```bash
set -euo pipefail
API=${API:-http://localhost:8000}
TOKEN=$(curl -sf "$API/v1/auth/login" -H 'content-type: application/json' -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" | jq -r .access_token)
curl -sf "$API/v1/chat" -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"question":"What is the policy on dogs in the lobby?","top_k":3}' | jq '{answer, sources: (.sources | length)}'
```

### Python (httpx)

<!-- verify:python -->
```python
import os
import time

import httpx

API = os.environ.get("API", "http://localhost:8000")
client = httpx.Client(base_url=API, timeout=60)

token = client.post("/v1/auth/login", json={"email": os.environ["EMAIL"], "password": os.environ["PASSWORD"]}).json()["access_token"]
client.headers["Authorization"] = f"Bearer {token}"  # or: client.headers["X-API-Key"] = "rag_..."


def check(r: httpx.Response) -> httpx.Response:
    if r.is_error:
        err = r.json()  # {"detail", "code", "request_id"}
        raise RuntimeError(f"{r.status_code} {err['code']}: {err['detail']} (request {err['request_id']})")
    return r


caps = check(client.get("/v1/capabilities")).json()
assert "hybrid" in caps["retrieval"]["modes"]

body = b"# Pets\n\nDogs under 20 kg are allowed in the lobby.\n"
up = check(client.post("/v1/documents", files={"file": ("pets.md", body, "text/markdown")})).json()
doc_id, job_id = up["document"]["id"], up["job"]["id"]
try:
    while (job := check(client.get(f"/v1/jobs/{job_id}")).json())["status"] not in ("done", "failed"):
        time.sleep(2)
    assert job["status"] == "done", job["error"]

    hits = check(client.post("/v1/search", json={"query": "dogs in the lobby", "top_k": 3, "document_ids": [doc_id]})).json()
    for hit in hits["results"]:
        print(hit["rank"], hit["filename"], hit["score_kind"], hit["text"][:50])

    offset, seen = 0, 0  # walk every page
    while True:
        page = check(client.get("/v1/documents", params={"limit": 100, "offset": offset})).json()
        seen += len(page["items"])
        if not page["has_more"]:
            break
        offset += page["limit"]
    assert seen == page["total"]
finally:
    check(client.delete(f"/v1/documents/{doc_id}"))
```

### JavaScript (fetch, Node 18+ or a browser)

<!-- verify:javascript -->
```javascript
const API = process.env.API ?? "http://localhost:8000";

async function call(path, { token, ...init } = {}) {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(init.headers ?? {}) },
  });
  if (!res.ok) {
    const err = await res.json(); // { detail, code, request_id }
    const detail = typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail);
    throw new Error(`${res.status} ${err.code}: ${detail} (request ${err.request_id})`);
  }
  return res.status === 204 ? null : res.json();
}

const { access_token: token } = await call("/v1/auth/login", {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ email: process.env.EMAIL, password: process.env.PASSWORD }),
});

const form = new FormData();
form.append("file", new Blob(["# Pets\n\nDogs under 20 kg are allowed in the lobby.\n"], { type: "text/markdown" }), "pets.md");
const { document, job } = await call("/v1/documents", { method: "POST", token, body: form });
try {
  let status;
  do {
    await new Promise((r) => setTimeout(r, 2000));
    status = (await call(`/v1/jobs/${job.id}`, { token })).status;
  } while (status !== "done" && status !== "failed");
  if (status !== "done") throw new Error("ingest failed");

  const found = await call("/v1/search", {
    method: "POST",
    token,
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ query: "dogs in the lobby", top_k: 3, document_ids: [document.id] }),
  });
  console.log(found.results.map((h) => `${h.rank} ${h.filename} ${h.score_kind}`));
} finally {
  await call(`/v1/documents/${document.id}`, { method: "DELETE", token });
}
```

### Service account (API key)

Create a narrow key as an admin, use it, revoke it (see [API keys](#authentication-for-services-api-keys)):

<!-- verify:apikey -->
```bash
set -euo pipefail
API=${API:-http://localhost:8000}
H='content-type: application/json'
TOKEN=$(curl -sf "$API/v1/auth/login" -H "$H" -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" | jq -r .access_token)
ROLE=$(curl -sf "$API/v1/roles" -H "Authorization: Bearer $TOKEN" -H "$H" -d '{"name":"api-docs-example-search-only","description":"example","permissions":["search:use"]}' | jq -r .id)
CREATED=$(curl -sf "$API/v1/api-keys" -H "Authorization: Bearer $TOKEN" -H "$H" -d "{\"name\":\"api-docs-example\",\"role_id\":\"$ROLE\",\"expires_in_days\":1}")
KEY=$(echo "$CREATED" | jq -r .key); KEY_ID=$(echo "$CREATED" | jq -r .id)

curl -sf "$API/v1/search" -H "X-API-Key: $KEY" -H "$H" -d '{"query":"anything","top_k":1}' -o /dev/null -w 'search with key: %{http_code}\n'
curl -s  "$API/v1/documents" -H "X-API-Key: $KEY" -o /dev/null -w 'documents with key: %{http_code} (403 = outside its role)\n'

curl -sf -X POST "$API/v1/api-keys/$KEY_ID/revoke" -H "Authorization: Bearer $TOKEN" -o /dev/null
curl -s "$API/v1/search" -H "X-API-Key: $KEY" -H "$H" -d '{"query":"x"}' -o /dev/null -w 'after revoke: %{http_code} (401)\n'
curl -sf -X DELETE "$API/v1/roles/$ROLE" -H "Authorization: Bearer $TOKEN" -o /dev/null   # clean up the example role
```

## Headless deployment (API only)

The dashboard is optional.

- **Compose:** `make up-api` starts everything except the dashboard container (`make up` adds it: the `frontend` service is in the
  `ui` profile; with plain `docker compose` add `--profile ui`).
- **Kubernetes:** `kubectl apply -k k8s/overlays/headless` deploys the API, workers, beat and stores without the frontend and
  routes the Ingress straight to the API (edit the host in `k8s/overlays/headless/ingress.yaml`). To drop the dashboard from your
  own overlay, add `components: [../../components/headless]` (do not combine it with the `prod` overlay, which patches the
  frontend).
- Set `CORS_ALLOW_ORIGINS` if a browser app on another origin will call the API; otherwise leave it empty.

## Compatibility and the contract test

[`openapi.json`](./openapi.json) is generated from the app (`make openapi`) and committed. `backend/tests/unit/test_openapi_contract.py`
compares the running app to it and fails on:

- a removed path or method; a removed request parameter or body field; a request parameter or field that became required (or a new
  required one); a removed 2xx response; a removed or retyped response field;
- any other drift (the snapshot must be regenerated and committed, so API changes are always visible in review).

Adding paths, optional request fields and response fields is non-breaking. To make an **intended** breaking change, list the exact
message printed by the test in [`openapi-breaking-ack.json`](./openapi-breaking-ack.json) (a JSON list of strings, kept as the audit
log of accepted breaks), then run `make openapi`; the export refuses to overwrite the snapshot while a break is unacknowledged.
Limits: the check works on the schema, so changed semantics with the same shape are not detected, and anyone can hand-edit the
snapshot, so reviewers should look at `docs/openapi.json` and the ack file in every diff.

## Runtime settings (`/v1/settings`)

Admin API behind the Settings window (ADR-0008). `GET` needs `settings:read` or `settings:manage`; everything
else needs `settings:manage` (API keys can never hold it).

| Call | Purpose |
|---|---|
| `GET /v1/settings` | Every setting: effective `value` (null for secrets, see `is_set`), `default`, `source` (`env` > `ui` > `config` > `default`), `locked` + `locked_reason`, `apply` (`live`, `restart`, `reindex`, `bootstrap`), `restart_pending`, who/when; plus a `summary` (secrets enabled, restart-pending keys, first-run `missing` hints). |
| `PATCH /v1/settings` | `{"set": {key: value}, "reset": [key], "confirm_reindex": false, "dry_run": false}`. All or nothing. 422 with one entry per bad key (`loc: ["settings", key]`); 409 `setting_locked` (pinned by env), `setting_not_editable` (bootstrap), `secrets_disabled`, `reindex_confirmation_required`. With `confirm_reindex` the value is applied and the reindex queued. |
| `POST /v1/settings/{key}/reset` | Back to config.json / the default (clears a secret). |
| `POST /v1/settings/test/{service}` | `llm`, `embeddings`, `vector_store`, `storage`, `parser`: cheap connectivity check of the saved settings. |
| `GET /v1/settings/reindex`, `POST /v1/settings/reindex` | Reindex state (Celery) and index drift problems; (re)queue one (`{"confirm_reindex": true}`). |
| `GET /v1/settings/audit?limit&before&key` | Change history. Secret rows carry no values. |

```bash
curl -s -X PATCH $API/v1/settings -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"set": {"default_top_k": 8, "openrouter_api_key": "sk-or-..."}}'
# the next request already uses top_k 8; the key is stored encrypted and never returned
```

## `POST /search` - ranked passages, no LLM

Permission: `search:use`. Never calls an LLM and needs no LLM key, so it keeps working when the LLM
provider is down or rate-limited. It is the entry point for teams that bring their own UI or their own LLM and
only want the best passages from the indexed documents.

It runs **exactly the retrieval path of `POST /chat`** (same modes, filters, de-duplication, ranking). For the
same query, `mode`, `fusion`, `top_k` and `document_ids`, the `results` are the passages `/chat` would have
cited, in the same order.

### Request

| Field | Type | Default | Meaning |
|---|---|---|---|
| `query` | string, 1..4000 chars | required | Natural-language or keyword query. |
| `top_k` | integer 1..50 | `DEFAULT_TOP_K` | Number of hits. |
| `mode` | `semantic` \| `keyword` \| `hybrid` | `RETRIEVAL_MODE` | How chunks are found (see [configuration](configuration.md#retrieval-modes)). |
| `fusion` | `rrf` \| `weighted` | `RETRIEVAL_FUSION` | How `hybrid` merges its two lists. Validated in every mode, used only by `hybrid`. |
| `neighbors` | integer 0..5 | `0` | Also return N chunks before/after each hit under `results[].context`. Unlike `/chat`, the default is **0** (hits only) and does not follow `RETRIEVAL_NEIGHBORS`. Best effort within a 12,000-character budget; hits are never dropped for it. |
| `document_ids` | list of UUIDs | all documents | Restrict the search (every mode). |
| `include_text` | boolean | `true` | `false` returns ids, ranks and scores only (`text` is `null`), a cheap ranking probe. |

Invalid input (empty or over-long `query`, `top_k` outside 1..50, unknown `mode`/`fusion`, `neighbors`
outside 0..5, malformed UUIDs, ...) is `422` and is not traced.

### Response

```json
{
  "query": "waiting period after a foreclosure",
  "mode": "hybrid",
  "fusion": "rrf",
  "top_k": 2,
  "neighbors": 1,
  "returned": 2,
  "took_ms": 41,
  "results": [
    {
      "rank": 1,
      "document_id": "3f0c9c3e-6c53-4d4e-9f55-0b0f7a1d2a10",
      "chunk_index": 17,
      "filename": "chapter-b3-6.pdf",
      "text": "A borrower must wait seven years after a foreclosure ...",
      "score": 0.0325,
      "score_kind": "rrf",
      "semantic_rank": 1,
      "keyword_rank": 2,
      "is_hit": true,
      "context": [
        {"chunk_index": 16, "text": "..."},
        {"chunk_index": 18, "text": "..."}
      ]
    }
  ]
}
```

`fusion` is `null` unless `mode` is `hybrid`. `took_ms` is server-side retrieval time.

### Ordering guarantees

- `results` contains **hits only**, sorted best first. `rank` is `1..returned` with no gaps. Within one
  response the order is consistent with `score` (ascending for `score_kind` `distance`, descending for all
  others).
- `score` is **mode-specific** (`score_kind`: `distance` lower = closer; `bm25`, `ts_rank_cd`, `rrf`,
  `weighted` higher = better) and only comparable inside one response. `semantic_rank` / `keyword_rank`
  are the 1-based positions in each candidate list (hybrid only; otherwise `null`).
- **Neighbour context is grouped under its hit** (`results[].context`): in document order, with only
  `chunk_index` and `text`. A context chunk has **no score and no rank** (it was not retrieved) and never appears
  in `results`. A chunk that neighbours two hits is listed under the better-ranked hit only.

### Errors

| Status | When |
|---|---|
| 401 | missing/invalid/expired token (auth enabled) |
| 403 | the token's user lacks `search:use` (`chat:use` alone is not enough) |
| 422 | invalid request body |
| 503 | retrieval backend unavailable, or a keyword/vector index that does not match the configuration; the message names the fix (e.g. `python -m app.manage rebuild-fts` / `reindex`). Identical to `/chat`'s retrieval errors. |

### curl

```bash
TOKEN=$(curl -s localhost:8000/auth/login -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"..."}' | jq -r .access_token)

curl -s localhost:8000/search -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"query": "waiting period after a foreclosure", "mode": "hybrid", "top_k": 3, "neighbors": 1}' | jq
```

### Python

```python
import httpx

BASE = "http://localhost:8000"
token = httpx.post(f"{BASE}/auth/login", json={"email": "you@example.com", "password": "..."}).json()["access_token"]

resp = httpx.post(
    f"{BASE}/search",
    headers={"Authorization": f"Bearer {token}"},
    json={"query": "waiting period after a foreclosure", "mode": "hybrid", "top_k": 5, "neighbors": 1},
    timeout=30,
)
resp.raise_for_status()
for hit in resp.json()["results"]:
    print(hit["rank"], hit["filename"], hit["chunk_index"], hit["score_kind"], hit["score"])
    context = "\n".join(c["text"] for c in hit["context"])  # surrounding chunks, unscored
```

Pass the passages to your own LLM, or just use the ids: `include_text=false` returns
`(document_id, chunk_index)` and scores only.

### Getting the permission on an existing install

`search:use` is part of the built-in `admin`, `user` and `viewer` roles:

- **New installs** get it from the startup seed.
- **Existing installs** get it from Alembic migration `0007` (`python -m alembic upgrade head`, which the
  API image runs on deploy): it adds `search:use` to the built-in `user` and `viewer` roles once. `admin` is
  re-synced to the full catalogue on every start. The migration never removes anything and never touches
  custom roles; if an admin later removes `search:use` from a built-in role it stays removed.
- **Custom roles** are not changed: add `search:use` to them in the dashboard (Settings > Roles) or via
  `PATCH /roles/{id}`.

### What changed for `POST /chat` clients

Additive, with one nullability change:

- Every `sources[]` item gained `rank` (1-based among hits, `null` for context chunks) and `context_for`
  (`{document_id, chunk_index}` of the hit a context chunk accompanies, `null` for hits).
- **`score` is now `null` for context chunks** (`is_hit: false`), as are their `semantic_rank` /
  `keyword_rank`. Before, a context chunk repeated the score and ranks of the hit it accompanied, which was
  misleading (a "worse" chunk could sit above its hit). Clients that assumed `score` is always a number must
  handle `null`; hits are unchanged.
- `sources` stays one flat list in **prompt order** (citation `[n]` = position `n`): hits are in best-first
  order relative to each other, each context chunk sits next to its hit in reading order.

<!-- ===== Ticket 35: Authentication for services (API keys) ===== -->

## Authentication for services (API keys)

Vendored consumers and other systems should not log in with an email and password (30-minute JWTs, a human
account). Give each client an **API key** instead: a long-lived, revocable credential bound to a **role**.

| | |
|---|---|
| Format | `rag_<prefix>_<secret>`; the prefix is 8 characters `[a-z0-9]`, the secret is `secrets.token_urlsafe(32)` (256 bits) |
| Send as | `X-API-Key: <key>` **or** `Authorization: Bearer <key>` (a Bearer value starting `rag_` is a key; anything else is a JWT) |
| Permissions | exactly the bound role's permissions, **read from the database on every request**, minus the management permissions below |
| Create / list / revoke | `POST /api-keys`, `GET /api-keys`, `POST /api-keys/{id}/revoke`; permission `apikeys:manage` (built-in `admin` only) |
| Dashboard | Admin > API keys |

### Security model

- **The secret is shown once.** `POST /api-keys` returns the full key in `key`; only `sha256(secret)` is stored and no
  endpoint can show it again. Lost key: revoke it and create a new one.
- **Why SHA-256 and not scrypt/argon2?** Slow hashes protect low-entropy, human-chosen passwords. The secret here is
  256 random bits, so guessing it from the hash is infeasible whatever the hash speed, and a slow hash would add
  ~100 ms of CPU to every machine request. Lookup is by the indexed prefix; the hash is compared with
  `hmac.compare_digest` (constant time), and an unknown prefix still performs a comparison.
- **No oracle.** Malformed, unknown prefix, wrong secret, expired, revoked and "owner deactivated" all return the same
  `401 {"detail": "invalid or expired API key"}` with the same headers.
- **Immediate effect.** Role edits, revocation, expiry and owner deactivation / deletion apply on the very next request,
  including for keys already in use (nothing is cached or embedded in the key). Deleting the owner deletes their keys.
- **A key never exceeds its role**, and never holds management permissions. `users:manage`, `roles:manage` and
  `apikeys:manage` are *not* usable by keys even if the bound role has them: a leaked machine credential cannot mint
  keys, users or roles. Keys can only consume (documents, jobs, chat, search, traces).
- **Binding the `admin` role is allowed but warned** (the create response carries `warnings`): such a key can read, write
  and delete everything (but still not manage users/roles/keys). Prefer a narrow role (e.g. only `search:use`).
- **No privilege escalation by creation:** you can only bind a role whose permissions you hold yourself (`409` otherwise).
- **Never logged or traced.** Keys are not logged, not stored in traces and not echoed in errors. Traces show the caller
  as `key:<name>`; `GET /api-keys` shows the public prefix, role, last use, expiry and revocation, never a secret.
- `last_used_at` is written at most **once per minute per key** (one conditional UPDATE), so a busy client does not
  cause a database write per request; treat it as "used within the last minute", not as an access log.
- **Header precedence:** if `X-API-Key` is present it is authoritative: it is validated on its own, and a valid
  `Authorization` header cannot rescue a bad key (nor override a good one). A JWT in `X-API-Key` is rejected (401); a
  key in `Authorization: Bearer` is accepted.
- **Rate limiting per key is NOT included.** Revocation is immediate, but a valid key can call as fast as the
  service allows. Rate-limit at your gateway / ingress (e.g. by the `X-API-Key` header) if you need to.
- Requires `AUTH_ENABLED=true` (a key needs an owning user); with auth disabled `POST /api-keys` returns `409`.
- Existing installs: migration `0009` creates the `api_keys` table and grants `apikeys:manage` to the built-in `admin`
  role. Custom roles are unchanged.

### Endpoints

`POST /api-keys` (`apikeys:manage`)

```json
{"name": "billing-service", "role_id": "<role id>", "expires_in_days": 90}
```

`expires_in_days` is optional (1-3650; omit for no expiry). `201`:

```json
{"id": "...", "name": "billing-service", "prefix": "k3j9x0aa", "role": "retrieval-only", "role_id": "...",
 "owner_email": "admin@example.com", "created_at": "...", "expires_at": "...", "last_used_at": null,
 "revoked_at": null, "status": "active",
 "key": "rag_k3j9x0aa_<43 random characters>",
 "warnings": []}
```

`key` is shown **once**. `GET /api-keys` returns the same objects without `key`/`warnings`; `status` is `active`,
`expired` or `revoked`. `POST /api-keys/{id}/revoke` is idempotent (a second call keeps the first `revoked_at`) and
keeps the row for audit; there is no `DELETE`. A role cannot be deleted while it has non-revoked keys (`409`).

### Recipe: a retrieval-only service account

```bash
API=http://localhost:8000
TOKEN=$(curl -s $API/auth/login -H 'content-type: application/json' \
  -d '{"email":"admin@example.com","password":"..."}' | jq -r .access_token)

# 1. a role that can only search
ROLE=$(curl -s $API/roles -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"retrieval-only","description":"search only","permissions":["search:use"]}' | jq -r .id)

# 2. a key bound to it (expires in 90 days); the secret is printed once
CREATED=$(curl -s $API/api-keys -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d "{\"name\":\"billing-service\",\"role_id\":\"$ROLE\",\"expires_in_days\":90}")
KEY=$(echo "$CREATED" | jq -r .key); KEY_ID=$(echo "$CREATED" | jq -r .id)

# 3. the service calls /search with it (either header works)
curl -s $API/search -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"query":"What is the minimum credit score for a conventional loan?","top_k":3}'
curl -s $API/search -H "Authorization: Bearer $KEY" -H 'content-type: application/json' -d '{"query":"..."}'

# anything outside the role is 403, e.g.  curl -X DELETE $API/documents/<id> -H "X-API-Key: $KEY"

# 4. revoke; the next call is 401
curl -s -X POST $API/api-keys/$KEY_ID/revoke -H "Authorization: Bearer $TOKEN"
```

```python
import httpx

client = httpx.Client(
    base_url="http://localhost:8000", headers={"X-API-Key": "rag_k3j9x0aa_..."}, timeout=30
)

r = client.post("/search", json={"query": "late fees on a conforming loan", "top_k": 3})
r.raise_for_status()
for hit in r.json()["results"]:
    print(hit["rank"], hit["filename"], hit["text"][:80])

# 401 -> key unknown, expired or revoked (one message for all); 403 -> the role lacks the permission
```

Store the key in your secret manager (a Kubernetes `Secret`, not in the image or in git) and rotate by creating a
second key, deploying it, then revoking the first.
<!-- ===== end Ticket 35 ===== -->

## Query cache headers and endpoints

`POST /search` and `POST /chat` answer with `X-Cache: hit|miss|bypass|off` and, per layer, `X-Cache-Layers: embedding=hit,retrieval=miss,...`; the JSON body has the
same verdict in `cache`. Send `"cache": false` (or `Cache-Control: no-cache`) to bypass it for one request. `GET /cache/stats?days=7` (permission `traces:read`)
and `POST /cache/purge` (permission `cache:manage`). See [`configuration.md`](./configuration.md#query-cache).


<!-- ===== Ticket 38: Chat streaming (SSE) ===== -->

## Streaming chat: `POST /chat/stream`

Same request body, same permission (`chat:use`), same retrieval, same prompt and same answer cache as
`POST /chat`, but the answer arrives token by token as Server-Sent Events (`Content-Type: text/event-stream`).
`POST /chat` is unchanged. Use `curl -N` (no buffering) or `fetch` + `ReadableStream`; the browser `EventSource`
API cannot be used because it cannot send an `Authorization` header (and only does GET).

```bash
curl -N -X POST http://localhost:8000/chat/stream \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question": "What does chapter 2 say about ...?"}'
```

### Before the stream starts: plain HTTP errors

Everything that can fail cheaply happens before the first byte, so these are ordinary JSON errors (`{"detail": ...}`),
not SSE events: `401` (no/invalid credentials), `403` (missing `chat:use`), `422` (invalid body), retrieval
failures (`503`/`502`, as on `/chat`) and a missing/invalid LLM configuration (`503`). The answer-cache lookup
also happens here, so the response headers are accurate: `X-Cache: hit|miss|bypass|off` and `X-Cache-Layers`
(identical semantics to `/chat`, including `cache: false` and `Cache-Control: no-cache`).

### Events

Each event is `event: <name>` + one `data: <single-line JSON>` + a blank line. Order: `sources`, then zero or more
`token`, then **exactly one** terminal event, `done` or `error` (never both, never a half-open stream), unless the
client disconnects.

| Event | `data` | Notes |
|---|---|---|
| `sources` | `{"sources": [SourceOut...], "cache": "hit\|miss\|bypass\|off"}` | First event, as soon as retrieval is done. `sources[]` has exactly the shape of `/chat` `sources` (citation `[n]` = position `n`). |
| `token` | `{"text": "<delta>"}` | Append to the answer. A cache hit is replayed as ~32-character `token` events (no artificial delay); "no relevant documents" is also sent as `token`s. |
| `done` | `{"answer", "usage": {"prompt_tokens", "completion_tokens"}, "cost_usd", "cache", "trace_id"}` | `answer` is the final, trimmed text (prefer it over your concatenation). `usage` and `cost_usd` follow `/traces` semantics: `cost_usd` is `null` when unknown (never `0` for "unknown"); a cache hit reports `0`/`0`/`0.0`. `trace_id` is the `GET /traces/{id}` row. |
| `error` | `{"status": 502\|503\|500, "detail", "trace_id"}` | The LLM failed after the stream opened (provider timeout/5xx/rate limit = `503`, rejected request = `502`, anything unexpected = `500` with a generic `detail`). Tokens already sent may be a partial answer: discard or label it. |

```
event: sources
data: {"sources":[{"text":"...","document_id":"...","chunk_index":3,"filename":"ch2.pdf","score":0.21,...}],"cache":"miss"}

event: token
data: {"text":"The standard "}

event: token
data: {"text":"requires [1]."}

event: done
data: {"answer":"The standard requires [1].","usage":{"prompt_tokens":812,"completion_tokens":14},"cost_usd":0.0,"cache":"miss","trace_id":"..."}
```

### Behaviour worth knowing

- **Cache:** a hit replays the stored answer with no LLM call (`X-Cache: hit`). A miss streams live and the full
  answer is stored **only after the provider finished cleanly**; a failure or a client disconnect stores nothing.
  Streamed and non-streamed requests share the same entries. Unlike `/chat`, two concurrent identical misses are not
  coalesced (each calls the LLM).
- **Cancellation:** if the client disconnects (closes the connection, `AbortController.abort()`, Ctrl-C in curl) the
  upstream LLM request is cancelled at the next chunk and nothing is cached. The trace is still written
  (`status: error`, `error: client_disconnected`, `meta.cancelled: true`).
- **Tracing / cost:** one `chat` trace row (with `meta.streamed: true` and `meta.ttft_ms`, the time to the first
  token) and one LLM generation per request, written when the stream ends. Prompt/completion tokens and the cost
  are captured from the provider's final usage chunk (OpenRouter: `usage.include`), as on `/chat`. A failed call
  (including an empty answer from a reasoning model that ran out of output tokens) keeps the usage the provider
  reported on its `error` trace; `meta.embedding` (`{tokens, cached?}`) holds the query-embedding usage. These are
  additive `meta` keys: the `/traces` schema is unchanged.
- **Proxies:** the response sets `Cache-Control: no-cache` and `X-Accel-Buffering: no`; put no response buffering
  or compression middlebox in front of it, or tokens will arrive in bursts.
- **Not included:** resumable streams (`Last-Event-ID`), WebSockets, streaming for `/search`.
