# 0004 — Query cache: three fail-open Redis layers

- Status: **Accepted**
- Date: 2026-11-15

## Context

Every `/search` and `/chat` embeds the query (Ollama round trip), searches the vector store (+ BM25), and `/chat` then calls an LLM
(seconds and money). Real traffic repeats questions. A cache must never make answers stale or wrong, and must never be a new way for the API to fail.

## Decision

Three layers in the **existing Redis** (own db 12, namespace `rag:cache:v1:`, never the Celery dbs; startup error if configured to the Celery db):

1. **L1 query embedding** — key: embedding model + query (NFC, trimmed, whitespace collapsed, **case preserved**). Value: packed float64 (bit-identical to uncached).
2. **L2 retrieval** — key: query, `top_k`, neighbours, sorted document filter, only the retrieval options relevant to the mode, a fingerprint of vector-store/embedding/chunking settings, and the **index version**.
3. **L3 answer** — key: question + hash of the *exact* prompt including retrieved context + model + system prompt. An answer is therefore never reused for a different context.

Invalidation: a random **index-version token** replaced after every change to what is searchable (ingest end, delete, replace, reindex, rebuild-fts/index, purge). Old entries become unreachable and expire by TTL. A random token (not a counter) means an evicted or reset key can never resurrect old entries.

Safety: **fail-open** (per-operation timeout, circuit breaker; Redis down means uncached, never an error); errors, empty answers and "no context" refusals are never cached; auth always runs before any lookup and keys contain **no user identity** (shared corpus); per-request bypass (`cache:false` / `Cache-Control`); in-process single-flight for identical concurrent misses; size cap.

Visibility: `X-Cache` headers, `cache` in responses and trace meta, `GET /cache/stats`, `POST /cache/purge` (`cache:manage`), Usage > Cache tab.

## Rejected / not built

- **Semantic (near-duplicate) cache** — trades correctness for hits; a wrong hit is worse than a miss. Revisit only with a measured false-hit rate on paraphrase pairs.
- **Case-folded keys** — embedding models are case sensitive; folding could return another query's vector.
- **A cache table in Postgres / in-process LRU only** — Redis is already deployed, is shared across replicas, and survives API restarts.
- **Counter-based version** — an evicted counter restarts at 0 and could match old entries.

## Consequences

- Measured on real data (`docs/performance.md`): median `/search` ~50 ms -> ~5 ms on a repeating workload, <= ~4 ms overhead on all-unique queries; cached `/chat` ~25 ms vs ~3.2 s.
- **Known limits:** a *different* process that fails to bump the version relies on the TTL (default 10 min) or a manual purge; single-flight is per process, so one compute per replica, not per cluster; if per-user document ownership is ever added, owner/ACL scope must join the L2/L3 keys.
- Test suites run with `CACHE_ENABLED=false` (they share a dev Redis); cache tests opt in on db 13.
