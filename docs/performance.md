# Performance

Everything here was measured on one Apple M4 laptop (16 GB, Docker Desktop VM 7.7 GB, local Ollama for embeddings) against the real Fannie Mae Selling Guide
chapters B3-4 and B3-5 (2 documents, 777 chunks) unless a row says otherwise. These are laptop numbers: useful for "what does it feel like" and for relative
comparisons, not capacity planning. Raw data and methods are linked from each section. The longer cache write-up is further down.

## At a glance (plain language)

- **Repeat questions are about 10x faster.** A repeated search drops from ~50 ms to ~5 ms (median), and the cache never serves a stale answer: ingest, delete, replace and reindex invalidate it immediately.
- **A cached chat answer comes back in ~25 ms instead of ~3 seconds, and costs nothing.** 180 of 180 repeated questions were served from cache with no LLM call.
- **The cache costs almost nothing when it misses.** With 100% unique questions it adds under 4 ms, and if Redis goes down the app keeps answering (uncached) in ~50 ms without errors, then recovers on its own in about 10 s.
- **A 50-page PDF is searchable in about 20 seconds.** Two chapters (42 and 53 pages, 777 chunks) finished ingesting in ~21 s each, in parallel, with local embeddings and no OCR cost: the PDF text layer is read in under a second.
- **A chat answer costs a few thousandths of a cent.** One real answer on a small paid model was 348 tokens in / 174 out = $0.0000139, roughly 70,000 short answers per dollar. Embeddings on the local model are free.
- **Streaming shows sources in a fraction of a second.** Sources appear in 0.1-0.25 s; the first answer token takes ~1 s on a small model (it was ~5 s on the free reasoning model, which thinks silently first).
- **It scales out on Kubernetes by itself.** In a local kind cluster the autoscaler grew the API from 2 to 8 pods under load and served 13,254 searches in 4 minutes with zero errors, then shrank back in ~3m40s.
- **Settings changes reach every replica in under 3 seconds** (measured 2.1-2.9 s across two API pods), no restart.
- **Editing a document re-embeds only what changed.** Content-defined chunking kept 46% of chunks identical between two real versions of a chapter, where fixed-size chunking kept 1%, so a small edit does not mean re-embedding the whole document.
- **Small and well tested.** The API idles at ~170-190 MB and peaked at ~360 MB under that load; 1,400+ backend and 300+ frontend tests pass, and the key tests were mutation-checked (break the code, watch the test fail).

## Raw numbers

| What | Number | Conditions | Source |
|---|---|---|---|
| `/search` median latency, semantic / hybrid, cache off | 50.1 / 49.2 ms | 1 client, top_k 5 | cache benchmark (below) |
| `/search` median latency, keyword (BM25), cache off | 9.7 ms | 1 client | cache benchmark |
| `/search` median latency, cache on, repeating workload | 5.1 / 4.5 / 4.3 ms (semantic / hybrid / keyword) | ~80% repeats (Zipf 1.1), cold start included | cache benchmark |
| `/search` throughput, cache off -> on | 19.7 -> 77.8 req/s (semantic), 20.1 -> 75.1 (hybrid), 104 -> 183 (keyword) | 1 client | cache benchmark |
| `/search` at 8 concurrent clients, cache off -> on | p50 164 -> 19.4 ms (semantic), throughput 46 -> 192 req/s | 8 clients | cache benchmark |
| Cache miss overhead | +0.3 ms keyword, +1.6 ms semantic, +3.9 ms hybrid (p50) | all-unique queries, 1 client | cache benchmark |
| `/chat` cold (real LLM) | median 3,173 ms, max 6,600 ms | 12 questions, free reasoning model | cache benchmark |
| `/chat` cache hit | p50 25 ms, p95 67 ms, p99 95 ms, 261 req/s | 180 requests, 8 clients, 100% hits | cache benchmark |
| Ingest, 42-page chapter (372 chunks) / 53-page chapter (405 chunks) | 20.6 s / 21.7 s end to end | both at once, native PDF text, local `mxbai-embed-large` | worker log, measured runrun |
| PDF text extraction, 42-53 pages | < 1 s | native text layer, no OCR | worker log |
| Retrieval quality, 12 labelled questions, top 5 | semantic 8/12, keyword BM25 10/12, hybrid RRF 9/12 (first-place: 7/7/7) | 388 chunks; 3 identical runs; a 1-2 question difference is not significant | measured runreport |
| Retrieval latency by mode (retriever only) | semantic 64 ms, BM25 17 ms, hybrid RRF 52 ms | Chroma; pgvector semantic 75 ms | measured runreport |
| Chat answer cost (paid model) | $0.0000139 for 348 in / 174 out tokens | meta-llama/llama-3.1-8b-instruct via OpenRouter | trace row, measured runsmoke |
| Streaming: sources / first token / done | 0.09-0.25 s / 0.98 s / 4.4 s (small paid model, in a kind cluster); first token ~4.5-5 s on the free reasoning model | `/chat/stream` | measured run|
| Kubernetes autoscaling | API 2 -> 7 -> 8 pods (cap), 13,254 requests in 240 s (~55 req/s), 0 errors, scale-down to 2 in ~3m40s | kind on the same laptop, cache bypassed | measured runkind report |
| API memory | 167 -> 193 MB over 240 requests (flat after the Chroma-client leak fix); peak ~360 MB under the autoscaler load | idle/steady vs load | measured run|
| Settings propagation across replicas | 2.1-2.9 s (limit 5 s) | 2 API pods | measured runsmoke |
| Chunk reuse between two real document versions | 46.3% identical (CDC) vs 0-1% (fixed-size) | Selling Guide B3-5, v1 vs v2 | measured runreport |

Honest limits: one machine, 777 chunks, synthetic repeat distribution, a free reasoning model for the cold chat numbers, and 12 labelled questions for retrieval quality.
Retrieval time grows with corpus size; the *saved* time (the embedding call) mostly does not. Memory is covered in the next section.

## Memory (measured after the memory work)

| What | Before | After | Notes |
|---|---|---|---|
| Worker idle | 1.05 GiB | 444 MiB | Celery now starts 2 child processes by default (`WORKER_CONCURRENCY`) instead of one per CPU core; add worker replicas to scale |
| 200-page scanned PDF, parse | OOM-killed at 4.4 GB | peak 276 MiB | pages are rendered and read one at a time; the 200-page text PDF peaks at 182 MiB |
| DOCX ingest (worker) | not measured | peak 213 MiB (+45 MiB) | torch/docling are not even imported at idle |
| API during 4 uploads | not measured | 158 -> 275 MiB | uploads stream in 1 MiB chunks |
| Chroma server idle (804 vectors) | 931 MiB | 68 MiB | after recreating it with the LRU index-cache config |
| Chroma after ingest + delete of a 200-page PDF, 4 cycles | 1.39 GiB | 233 MiB | upstream Chroma keeps deleted indexes in memory: restart it after a mass delete or reindex (documented) |
| Chroma under 10 min of search load (330k queries) | n/a | 59 -> 75 MiB | search itself does not grow memory |

Compose now caps chroma at 1.5 GB, the API at 1 GB and the worker at 3 GB, so one runaway container is killed and restarted alone instead of the Docker VM killing a random one.
Not measured: Chroma beyond ~3k vectors per collection (the sizing rule in `docs/configuration.md` is extrapolated), and the K8s limits were only tested structurally after this change.

# Query cache benchmark

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
