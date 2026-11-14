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
