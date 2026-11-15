# HTTP API reference

The interactive OpenAPI docs are served by the API itself at `/docs` (Swagger UI) and `/openapi.json`.
All routes except `/health` and `/auth/login|refresh` need `Authorization: Bearer <access token>` when
`AUTH_ENABLED=true` (the default in Compose); get a token from `POST /auth/login`.

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
