# Performance: query cache

Measured with `scripts/bench_cache.py` on the real stack (Docker Compose: API, worker, Postgres, Redis, Chroma server) and the real
Fannie Mae Selling Guide chapters B3-4 and B3-5 (2 documents, 777 chunks). Raw results: [`performance-cache-bench.json`](./performance-cache-bench.json).

## Setup (everything that could change the numbers)

| | |
|---|---|
| Machine | Apple M4, 16 GB, macOS; Docker Desktop VM 7.7 GB; Ollama on the host |
| Embeddings | `mxbai-embed-large` via Ollama (CPU/GPU of the same machine) |
| Vector store | Chroma server, cosine, `top_k=5`, neighbours 0 |
| LLM (chat only) | `nvidia/nemotron-3.5-super-vl:free` via OpenRouter (the free tier; latency varies with its load) |
| Workload | the 12 labelled questions in `tmp/smoke_tests/search-questions.json` (refusal items excluded), 60 requests per run drawn from a Zipf(s=1.1) distribution; every other request carries stray whitespace that the cache normalises away |
| Not in the workload | paraphrases: a rephrased question is a different cache key by design (there is no semantic cache) |
| Cache OFF | each request sent with `"cache": false` (no read, no write, same code path otherwise) |
| Cache ON | defaults (TTL 24 h embedding / 10 min retrieval / 1 h answer), **started cold** (`POST /cache/purge` before each run), so first-time misses are included in the ON numbers |
| Repetitions | 5 per condition, OFF and ON interleaved; the tables show the **median of the 5 run-level statistics** |
| Concurrency | 1 and 8 clients |
| Errors | 0 of all search requests |

## `/search`

Latency in ms, throughput in requests/s. "zipf" = repeating workload; "unique" = every query unique (the miss path: the worst case for the cache).

| mode / concurrency / workload | OFF p50 | ON p50 | OFF p95 | ON p95 | OFF rps | ON rps | ON hit rate |
|---|---|---|---|---|---|---|---|
| semantic, 1, zipf | 50.1 | 5.1 | 58.6 | 49.5 | 19.7 | 77.8 | 82% |
| semantic, 8, zipf | 164.0 | 19.4 | 171.2 | 106.4 | 46.4 | 192.3 | 67% |
| keyword, 1, zipf | 9.7 | 4.3 | 11.4 | 10.5 | 104.4 | 183.4 | 82% |
| keyword, 8, zipf | 39.6 | 23.2 | 81.1 | 63.3 | 168.9 | 247.9 | 78% |
| hybrid, 1, zipf | 49.2 | 4.5 | 56.3 | 51.9 | 20.1 | 75.1 | 82% |
| hybrid, 8, zipf | 165.4 | 22.5 | 185.1 | 115.0 | 46.7 | 179.0 | 67% |
| semantic, 1, unique | 48.3 | 49.9 | 56.1 | 56.4 | 20.2 | 19.9 | 0% |
| semantic, 8, unique | 165.7 | 166.0 | 173.7 | 189.3 | 46.0 | 45.8 | 0% |
| keyword, 1, unique | 9.7 | 10.0 | 11.9 | 12.2 | 101.7 | 99.2 | 0% |
| keyword, 8, unique | 40.7 | 43.6 | 84.3 | 92.1 | 163.0 | 145.9 | 0% |
| hybrid, 1, unique | 46.7 | 50.6 | 55.2 | 58.7 | 20.9 | 19.9 | 0% |
| hybrid, 8, unique | 166.9 | 166.8 | 173.6 | 209.0 | 45.5 | 45.3 | 0% |

What it shows:
- Semantic and hybrid search cost ~48 ms at 1 client, almost all of it the query embedding; keyword search needs no embedding (~10 ms).
- On a repeating workload the median drops by roughly the embedding cost, and throughput rises 3.5-4x for semantic/hybrid at the measured concurrency.
- The p95 improves much less than the p50 because the ON runs include the cold first request for each distinct question (every distinct query is a full-price miss once).
- The miss path costs little: with all-unique queries the ON/OFF p50 differ by about +0.3 to +4 ms (keyword +0.3 ms, semantic +1.6 ms, hybrid +3.9 ms at 1 client; at 8 clients the differences are within the run-to-run spread). One cache read and one write per layer on every miss.

## `/chat` (12 real LLM calls were made, no more)

| | |
|---|---|
| Cold (miss, real LLM), 12 questions, median | 3173 ms (max 6600 ms) |
| Warm (answer cache hit), 180 requests, 8 clients: p50 / p95 / p99 | 25 / 67 / 95 ms |
| Warm throughput | 261 requests/s, hit rate 100% |
| LLM calls avoided | 180 of 180 warm requests (each would also have spent credits; the model used here is free, so $ saved is 0 in this run) |

The cold figure is a single call per question against a free-tier model: it is a sample, not a distribution, and varies with the provider's load.

## What these numbers do NOT show

- One machine, two documents (777 chunks). Retrieval cost grows with corpus size, so absolute search latency would be higher on a large corpus; the *saved* time (the embedding call) mostly would not change.
- Hit rates depend entirely on the repeat distribution of real traffic. The workload here (12 questions, Zipf 1.1) is synthetic; do not extrapolate a hit rate to production.
- The embedding provider was local Ollama. With a hosted embedding API the saved time per hit would be a network round trip instead.
- Numbers are for a single API process. With several replicas each keeps its own in-process single-flight; Redis is shared.
- `/chat` OFF was not replayed in bulk (that would burn credits for no information): the cold miss *is* the OFF path.
- Freshness: ingest, delete, replace, reindex and purge invalidate retrieval and answer entries immediately (tests in `backend/tests/integration/test_query_cache_api.py` and `test_cache_invalidation.py`); the query-embedding layer is keyed by model and text only, so it never needs invalidation.

## How to quote this

> On a local benchmark (Apple M4, 2 Fannie Mae Selling Guide chapters, 777 chunks, Ollama `mxbai-embed-large`), the Redis query cache cut median `/search` latency from about 50 ms to about 5 ms (semantic and hybrid) on a workload where roughly 80% of requests repeat an earlier query, and raised single-client throughput from 20 to 75-78 requests/s. With 100% unique queries the overhead was under 4 ms at the median. Cached `/chat` answers returned in ~25 ms (p50) versus ~3.2 s for a real LLM call, with no LLM spend on hits.

Never quote a bare multiplier; always include the repeat ratio and what was measured.

## Reproduce

```bash
docker compose up -d --build            # stack + the two chapters ingested
cd backend && BENCH_EMAIL=... BENCH_PASSWORD=... uv run python ../scripts/bench_cache.py \
  --reps 5 --requests 60 --chat-cold 12 --out ../docs/performance-cache-bench.json
```

`--chat-cold 0` skips `/chat` (no LLM calls). The script needs an admin user (it calls `POST /cache/purge`).

## Bug found by this benchmark

The first run crashed the API container (OOM) after a few thousand searches: `ChromaStore` created a new `chromadb.HttpClient` for every request
(~1.5 MB each, never freed). Fixed by sharing one client per Chroma server (`_chroma_client` in `app/services/vectorstore.py`); API memory stays flat
(167 -> 193 MB over 240 requests, previously +60 MB per 40). This predates the cache and affected every `/search` and `/chat`.
