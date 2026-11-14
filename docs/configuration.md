# Configuration reference

Settings are layered: environment variables > `config/config.<RAG_ENV>.json` > defaults
(`backend/app/config.py`). Every choice is a flat key with a validated range; an invalid value
fails at startup with the allowed values. **Scope** says when a change takes effect:

- *query-time* — applies to the next request; may also be overridden per request.
- *index-time* — changes how documents are indexed; existing documents are re-chunked and
  re-embedded on their next ingest (the chunker fingerprint no longer matches).
- *reindex* — changes how the **vector store** is built (store, metric, index type/params,
  embedding dimension). The app detects the difference at startup and refuses to search a
  mismatched index; fix it with `python -m app.manage reindex` (see
  [Changing the vector store](#changing-the-vector-store)).

| Key | Values | Default | Scope | Meaning |
|---|---|---|---|---|
| `CHUNK_SIZE` | integer > 0 (chars) | `1000` | index-time | Hard **maximum** chunk length. Chunks average roughly 0.5 x this (~525 chars at 1000) and are cut at word starts, preferably at sentence ends / blank lines (ADR-0003). |
| `CHUNK_OVERLAP` | integer, `0 <= v < CHUNK_SIZE` (chars) | `100` | index-time | **Minimum** chunk length (nothing is repeated between chunks; the name is kept for compatibility). A smaller minimum resynchronises cut points faster after an edit but allows smaller chunks. |
| `RETRIEVAL_NEIGHBORS` | integer `0..5` | `1` | query-time (per-request `neighbors` on `POST /chat`) | Besides each search hit, also give the LLM the N chunks before and after it from the same document (de-duplicated, in reading order, bounded by the 12,000-char prompt context budget; hits always keep priority). `0` = hits only. Response `sources[].is_hit` is `false` for context-only chunks. |
| `DEFAULT_TOP_K` | integer >= 1 | `5` | query-time (per-request `top_k`) | Number of search hits (before neighbour expansion). |
| `RETRIEVAL_MODE` | `semantic`, `keyword`, `hybrid` | `semantic` | query-time (per-request `mode` on `POST /chat`) | How chunks are found. `semantic` = embeddings only (the behaviour before ticket 23). `keyword` = Postgres full-text search with BM25 re-scoring, no embedding call. `hybrid` = both, fused. See [Retrieval modes](#retrieval-modes). |
| `RETRIEVAL_KEYWORD_METHOD` | `bm25`, `postgres_fts` | `bm25` | query-time | `bm25`: true BM25 over an FTS candidate pool. `postgres_fts`: plain `ts_rank_cd` (faster, weaker ranking). Used by `keyword` and `hybrid`. |
| `RETRIEVAL_FUSION` | `rrf`, `weighted` | `rrf` | query-time (per-request `fusion`) | How `hybrid` merges the two ranked lists. Ignored (but validated) in other modes. |
| `RETRIEVAL_RRF_K` | integer `1..1000` | `60` | query-time | RRF constant: `score = sum 1/(k + rank)`. Larger = flatter (ranks matter less), smaller = the top ranks dominate. 60 is the value from the original RRF paper. |
| `RETRIEVAL_WEIGHT_SEMANTIC`, `RETRIEVAL_WEIGHT_KEYWORD` | floats `>= 0`, not both 0 | `0.6`, `0.4` | query-time | Weights of `weighted` fusion. **Auto-normalised** to sum 1 (only their ratio matters: `3`/`1` = `0.75`/`0.25`); not required to sum to 1. |
| `RETRIEVAL_CANDIDATES` | integer `1..500` | `50` | query-time | Size of each retriever's candidate list before fusion / the `top_k` cut (raised to `top_k` when that is larger). Also the over-fetch that lets de-duplication fill `top_k`. |
| `RETRIEVAL_KEYWORD_POOL` | integer `1..5000` | `200` | query-time | BM25 only: how many FTS matches (best `ts_rank_cd` first) are re-scored with BM25. The one approximation of the keyword path. |
| `BM25_K1` | float `0..10` | `1.2` | query-time | Term-frequency saturation (0 = tf ignored). Lucene/Elasticsearch default. |
| `BM25_B` | float `0..1` | `0.75` | query-time | Length normalisation (0 = none, 1 = full). Lucene/Elasticsearch default. |
| `RETRIEVAL_FTS_CONFIG` | Postgres text-search configuration name, e.g. `english`, `simple`, `german` | `english` | index-time (`python -m app.manage rebuild-fts`) | Tokenisation/stemming/stopwords of the keyword index (`chunks.fts`). See [Keyword index](#keyword-index-and-tokenisation). |
| `RETRIEVAL_DEDUPE` | `true`, `false` | `true` | query-time | Keep only the best-ranked copy of chunks with identical text (duplicate uploads), in every mode. With `false`, `semantic` asks the vector store for exactly `top_k` (the pre-ticket-23 request). |
| `VECTOR_STORE` | `chroma`, `pgvector` | `chroma` | reindex | Where vectors live. Chroma = the Chroma server (default, unchanged behaviour). pgvector = a table in the app's Postgres (`vector_items`, created automatically). |
| `VECTOR_METRIC` | `cosine`, `l2`, `ip` | unset = store default: `l2` for Chroma, `cosine` for pgvector | reindex (pgvector with `VECTOR_INDEX=none`: query-time) | Distance function. Unset on Chroma keeps the metric an existing collection already has (Chroma's default is l2), so upgrading changes nothing. For length-normalised embeddings (mxbai-embed-large is) all three give the same ranking; the choice only matters for non-normalised models (`ip`) and for the score values. |
| `VECTOR_INDEX` | `hnsw`, `ivfflat`, `none` | `hnsw` | reindex | ANN index. Chroma only has HNSW (`ivfflat`/`none` with `VECTOR_STORE=chroma` fail at startup). pgvector: `none` = exact scan (always correct, fine up to a few 10k vectors). |
| `HNSW_M` | integer `2..100` | pgvector 16; Chroma: left to Chroma (16) | reindex | Graph connectivity (build time + memory vs recall). |
| `HNSW_EF_CONSTRUCTION` | integer `4..1000`, `>= 2 * HNSW_M` | pgvector 64; Chroma: left to Chroma (100) | reindex | Build-time candidate list size (build time vs recall). |
| `HNSW_EF_SEARCH` | integer `1..1000` | pgvector 40; Chroma: left to Chroma (100) | query-time | Search-time candidate list (latency vs recall). pgvector raises it to `top_k` when `top_k` is larger. Chroma applies it to the existing collection at startup. |
| `IVFFLAT_LISTS` | integer `1..32768` | `100` | reindex | Number of clusters. The index is only built once the table has at least this many rows (see below). Rule of thumb: rows/1000 up to 1M rows, sqrt(rows) above. |
| `IVFFLAT_PROBES` | integer `1..IVFFLAT_LISTS` | `10` | query-time | Clusters searched per query (latency vs recall). `= IVFFLAT_LISTS` is an exact search. |
| `EMBEDDING_DIMENSION` | integer | unset = known model table, else one probe call | reindex | Vector length for the pgvector column (never hard-coded). Known: mxbai-embed-large 1024, nomic-embed-text 768, text-embedding-3-small 1536, ... Set it for any other model so no embedding call is needed at startup. |
| `CHROMA_COLLECTION` | string | `documents` | deployment | Collection holding the vectors, so several deployments can share one Chroma server. |
| `STORAGE_BACKEND` | `local`, `s3` | `local` | deployment (per document: `documents.storage_backend`) | Where uploaded / downloaded source files are kept. `local` = `DATA_DIR/uploads/<id>/<name>`. `s3` = any S3-compatible store through boto3. Existing documents stay where they were written. See [File storage and URL ingest](#file-storage-and-url-ingest). |
| `S3_ENDPOINT_URL` | http(s) URL or empty | empty (= AWS) | deployment | Endpoint of an S3-compatible store (MinIO: `http://minio:9000`). Path-style addressing is used when set. |
| `S3_BUCKET` | string | empty | deployment | Bucket for `STORAGE_BACKEND=s3` (required then) and the default bucket `s3://` links may read. |
| `S3_REGION` | string | `us-east-1` | deployment | Signing region. |
| `S3_PREFIX` | key prefix | `rag-pipeline/` | deployment | Everything the app writes lives under `<prefix>uploads/<32 hex>/<name>`; it never lists, reads (storage) or deletes anything outside it. |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | strings (**secrets**) | empty (= boto3 default chain / IAM role) | deployment | Must be set together. `.env` / K8s Secret only, never in `config.<env>.json`. |
| `URL_INGEST_SCHEMES` | comma list of `http`, `https`, `s3` | `http,https,s3` | deployment | Schemes `POST /documents/from-url` (and every redirect hop) may use. |
| `URL_INGEST_S3_BUCKETS` | comma list of bucket names | empty (= only `S3_BUCKET`) | deployment | Buckets an `s3://` link may read. The server's credentials are used, so keep this list tight. |
| `URL_INGEST_ALLOW_PRIVATE` | `true`, `false` | `false` | deployment | **Tests / smoke runs only.** `true` disables the private/loopback/link-local/metadata address block so a local test server can be reached. Never set it in production. |
| `URL_INGEST_TIMEOUT_SECONDS` | `0 < v <= 600` | `20` | deployment | Connect / read timeout of each download request. |
| `URL_INGEST_TOTAL_TIMEOUT_SECONDS` | `>= URL_INGEST_TIMEOUT_SECONDS`, `<= 3600` | `120` | deployment | Overall deadline of one download (defeats slow-drip servers). |
| `URL_INGEST_MAX_REDIRECTS` | integer `0..20` | `5` | deployment | Redirect hops followed (each one is re-validated). |
| `URL_INGEST_MAX_BYTES` | integer `>= 1` or unset | unset = `MAX_UPLOAD_SIZE_BYTES` | deployment | Download size cap, enforced on the bytes actually received. |

Why these defaults: see "Parameter choice" in `docs/adr/0003-chunking.md`.

## Vector store and index options

### What each store honours (verified on Chroma server 1.4.4 + `chromadb-client` 1.5.9, pgvector 0.8)

| Option | Chroma | pgvector |
|---|---|---|
| metric | `configuration.hnsw.space` at collection creation (**immutable**; Chroma silently ignores it for an existing collection, which is why the app reads it back and compares) | operator class of the index (`vector_cosine_ops` / `vector_l2_ops` / `vector_ip_ops`) |
| HNSW `M`, `ef_construction` | `max_neighbors`, `ef_construction` at creation (immutable) | `CREATE INDEX ... USING hnsw ... WITH (m, ef_construction)` |
| HNSW `ef_search` | `collection.modify(configuration={"hnsw": {"ef_search": n}})` — changeable any time; applied at startup | `SET LOCAL hnsw.ef_search` per query |
| IVFFlat / no index | not available | `lists`; `SET LOCAL ivfflat.probes` per query |
| Filters | `where` (equality, `$in`, `$and`) | SQL: `doc_id` column + `metadata @>` containment |
| Max dimensions with an index | n/a | 2000 (use `VECTOR_INDEX=none` above that) |

### Score convention: lower = closer, identical on both stores

`similarity_search` returns **distances** ordered ascending, with the same numbers on both stores:
`cosine` = `1 - cos`, `l2` = **squared** euclidean distance, `ip` = `1 - dot` (negative when the dot product
exceeds 1). pgvector's native operators differ (`<=>` cosine distance, `<->` plain euclidean, `<#>` the
*negative* dot product), so `PgVectorStore` uses the native operator in `ORDER BY` (so the index is usable)
and converts only the selected value (`power(x, 2)`, `1 + x`). A contract test pins the numbers on both stores.

### pgvector storage

Vectors live in their own table `vector_items(id, doc_id, text, metadata jsonb, embedding vector(N))`, created
idempotently (advisory-locked, safe for API + worker starting together) the first time the store is verified.
The old `chunks.vector` column is **not** used: chunk rows are deleted and recreated on every ingest, so vectors
kept by an incremental update would be lost, and that column is a hard-coded `vector(1024)`. Ingestion writes
vectors to exactly one store (the configured one).

### IVFFlat and empty/small tables

IVFFlat learns its cluster centres from the rows present when the index is built, so an index built on an empty
table is useless. The app therefore **defers** it: with fewer rows than `IVFFLAT_LISTS` it logs a warning, searches
are exact, and `/health` shows `pgvector cosine/ivfflat`. The index is built automatically after the ingest that
crosses the threshold (on whatever rows exist then) and is rebuilt on the full data by `reindex` or
`python -m app.manage rebuild-index`. Retrain after large changes in the data; for a corpus that keeps growing
prefer `hnsw`.

### What the planner does at small sizes

An index existing is not the same as it being used. With the planner's default settings Postgres ignores the HNSW
and IVFFlat indexes on small tables: on the real 388-chunk, 1024-dimension smoke data `EXPLAIN` shows a sequential
scan for both (the index is there, in `pg_indexes`, and is used on a 2,000-row synthetic table of 16-dimension
vectors; the tests EXPLAIN the exact SQL the store runs). So at a few hundred chunks pgvector answers are exact
regardless of `VECTOR_INDEX`, and recall/latency differences between the indexes only appear at tens of thousands
of rows and up.

## Changing the vector store

Everything under *reindex* above is baked into the stored index. The app reads the stored settings back (Chroma:
collection configuration and a stored vector's length; pgvector: the column type and `pg_indexes`) and compares
them with the configuration at API start, worker start and on first use. On a mismatch:

* the API logs an error naming each difference, `GET /health` reports `vector_settings` as not ok (informational,
  the API stays up), `POST /chat` and new ingests fail with **503 / a failed job** carrying the message, and
  deleting a document fails the same way until it is fixed (its vectors cannot be removed from a store that cannot
  be opened);
* an empty store while the database holds chunks (you switched `VECTOR_STORE`, or the store was wiped) is reported
  too.

Fix it by rebuilding from the chunk text already in Postgres (no re-parsing, no re-chunking, same vector ids, so
later incremental updates keep working):

```bash
python -m app.manage vector-status          # configured vs stored settings, vector count (exit 1 on a mismatch)
python -m app.manage reindex                # drop + recreate the store with the configured settings, re-embed everything
python -m app.manage reindex --queue        # same, as the Celery task app.tasks.reindex.reindex_vectors
python -m app.manage rebuild-index          # pgvector only: rebuild the ANN index on the current rows, no re-embedding
```

Pause ingestion first (`reindex` refuses while ingest jobs are running unless `--force`); search is unavailable or
partial while it runs. It costs one embedding call per stored chunk (about 17 s for 388 chunks on a local Ollama).
If it fails part-way, run it again. Changing only a **query-time** knob (`HNSW_EF_SEARCH`, `IVFFLAT_PROBES`,
`DEFAULT_TOP_K`) needs no reindex. Changing `EMBEDDING_MODEL` to one with a different dimension is detected by the
dimension check; swapping to a different model with the *same* dimension is not detectable from the store, so
reindex whenever you change the embedding model.

## Retrieval modes

`POST /chat` accepts optional `"mode": "semantic"|"keyword"|"hybrid"` and `"fusion": "rrf"|"weighted"` (anything else is a 422);
omitted fields use the deployment settings above. `document_ids` filters every mode. Neighbour expansion
(`RETRIEVAL_NEIGHBORS`) runs after retrieval, on whatever hits the mode produced.

### What `score` means (the response says so in `sources[].score_kind`)

Sources are always ordered best first. The number is **not comparable across modes**:

| `score_kind` | Mode | Meaning | Better |
|---|---|---|---|
| `distance` | `semantic` | vector-store distance (see the score convention below) | lower |
| `bm25` | `keyword` | BM25 value | higher |
| `ts_rank_cd` | `keyword` + `postgres_fts` | Postgres cover-density rank | higher |
| `rrf` | `hybrid` + `rrf` | `sum 1/(k + rank)` over the lists the chunk is in (max `2/(k+1)`) | higher |
| `weighted` | `hybrid` + `weighted` | `w_s * norm_s + w_k * norm_k`, 0..1 | higher |

In `hybrid`, `sources[].semantic_rank` / `keyword_rank` give the 1-based rank in each retriever's (de-duplicated) candidate list,
or `null` when the chunk was not among that retriever's candidates. Traces record `mode`, `keyword_method` (keyword/hybrid),
`fusion` (hybrid) and `candidates` in their `meta`.

### Hybrid

Both retrievers run with `RETRIEVAL_CANDIDATES` each (the vector search and the SQL search concurrently) and are fused:

* **RRF**: `score(d) = sum_i 1/(k + rank_i(d))`, 1-based ranks, missing from a list = no term. Ignores raw scores, so nothing has to be made comparable.
* **Weighted**: each list's raw scores are min-max normalised to `[0,1]` (best = 1; semantic distances are inverted first); a list whose
  scores are all equal, including a single item, maps every item to 1.0; then `w_s*norm_s + w_k*norm_k` with the weights normalised to sum 1.
  Because it is min-max over a candidate list, one extreme outlier flattens the rest; RRF has no such sensitivity, which is why it is the default.
* Ties are broken by first appearance (semantic list first), so output is deterministic.
* **De-duplication** identifies a passage by its whitespace-collapsed, case-folded text, so two uploads of the same document produce one
  item (that collects the contributions of both copies) instead of filling several slots. If the keyword list is empty (noise or
  stopword-only query, no matching term) the result is the semantic list alone.

### Keyword index and tokenisation

Migration `0005` adds `chunks.fts = to_tsvector('english', text)` and `chunks.fts_len` (token count) as *generated stored columns*
(Postgres maintains them; ingestion is untouched) and a GIN index on `fts`. The question is tokenised by Postgres with the same
configuration and the lexemes are OR-ed (an AND query, which `websearch_to_tsquery` would build, finds nothing for a natural-language question).

* **IDs and numbers.** Postgres splits `B3-5.3-05` into `b3`, `-5.3`, `-05` and keeps numbers (`12`, `500.00`, `2025`) in both `simple` and `english`.
  Matching the parts separately would rank `B3-5.3-04` almost as high as `B3-5.3-05`, so every ID-like token of the question (letters/digits joined by `- . / _`)
  is also searched as an adjacent-lexeme phrase and, for BM25, scored as one extra term (idf from its document frequency, tf = 1). Tested explicitly.
* **`english` vs `simple`.** Both keep IDs and numbers. `english` additionally drops stopwords (`what`, `does`, `the`) and stems (`waiting`, `waited` -> `wait`), which is
  what natural-language questions over English documents need; `simple` keeps every token verbatim and is the right choice for non-English corpora
  (or set the matching language, e.g. `german`). The default is `english` because the documents and questions this project targets are English.
* **Changing it** is index-time: the configuration is baked into the generated columns. The app compares it with `RETRIEVAL_FTS_CONFIG` (catalog lookup) and
  refuses keyword/hybrid searches with a 503 naming the fix instead of searching with the wrong tokenisation; the API also logs the mismatch at start.
  `python -m app.manage fts-status` shows configured vs stored; `python -m app.manage rebuild-fts` recreates the columns and index (exclusive lock on `chunks`
  while it runs, no re-embedding).

### BM25 precisely

`score(chunk) = sum over query terms t in chunk of idf(t) * tf (k1 + 1) / (tf + k1 (1 - b + b * len / avglen))`, with
`idf(t) = ln(1 + (N - df + 0.5) / (df + 0.5))` (non-negative). `N` = all chunks, `avglen` = average token count of all chunks, `df` = chunks
matching the term (exact, GIN-backed `count(*)`), `tf` = the term's position count in the chunk's tsvector, `len` = `fts_len`. All of these are computed
by SQL over the whole corpus (not over the `document_ids` filter, so a chunk's score does not change with the filter).

**The approximation:** the chunks that get a BM25 score are the top `RETRIEVAL_KEYWORD_POOL` (200) FTS matches by `ts_rank_cd`; a chunk BM25 would
rank highly but `ts_rank_cd` ranks below the cut is never seen (tested). idf, length normalisation and tf are exact. Other limits: stopwords are not
counted in lengths; Postgres stores at most 256 positions per lexeme (tf saturates there); at most 64 query terms are used (the rarest ones) and at most the
first 2,000 characters of the question; dropped terms with no match in the corpus cost nothing.

## File storage and URL ingest

### Storage backends

Source files are addressed by a key `uploads/<32 hex>/<file name>` stored in `documents.source_path`;
`documents.storage_backend` (`local` | `s3`) says which backend holds it, so switching `STORAGE_BACKEND` never strands
existing documents (they keep being read, replaced and deleted where they are). The parser still needs a local
file: for S3 the object is downloaded to a temp file for the duration of the parse.

* **Delete** (`DELETE /documents/{id}`) removes the stored file/object (best effort: a storage failure is logged and
  the scheduled cleanup removes the leftover). **Replace** (`PUT /documents/{id}`) stores the new file in the
  *configured* backend and removes the old version from the backend it was in.
* **Orphan cleanup** (daily Celery task, `CLEANUP_ORPHAN_AGE_HOURS`): local = `uploads/<id>` directories no document
  references. S3 (only when `STORAGE_BACKEND=s3`) = objects that are under `S3_PREFIX`, have exactly the shape
  `uploads/<32 hex>/<name>`, are referenced by no `s3` document and are older than the age limit. Anything else in
  the bucket (other prefixes, other key shapes, other tenants) is never listed for deletion.
* **Local MinIO**: `docker compose --profile s3 up -d minio` starts a real MinIO on `:9000` (console `:9001`) with
  bucket `$S3_BUCKET` (default `rag-documents`). The default `docker compose up` does not start it. MinIO no longer
  publishes images to Docker Hub/Quay, so the profile uses the last public Bitnami build (`MINIO_IMAGE` overrides it).
  Containers must reach it as `http://minio:9000`: set `S3_ENDPOINT_URL_DOCKER=http://minio:9000` (and
  `S3_ENDPOINT_URL=http://localhost:9000` for host-run tools/tests).
* **Kubernetes**: with `STORAGE_BACKEND=s3` the api and worker no longer share a ReadWriteMany uploads volume.

### Ingest from a link: `POST /documents/from-url`

```bash
curl -X POST http://localhost:8000/documents/from-url -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"url": "https://www.rfc-editor.org/rfc/rfc8259.txt"}'
# 201 {"document": {"id": "...", "filename": "rfc8259.txt", "source_url": "https://www.rfc-editor.org/rfc/rfc8259.txt", "status": "pending", ...},
#       "job": {"id": "...", "status": "pending", ...}}
curl http://localhost:8000/jobs/<job id> -H "Authorization: Bearer $TOKEN"
# stats: {"source_url": "...", "bytes_downloaded": 71, "storage": "local", "mode": "full", "chunks_total": ..., ...}
```

Permission `documents:write`. The same API serves the dashboard and any other client; the OpenAPI document (`/docs`)
describes the body, the `201` and the errors.

| Status | When |
|---|---|
| `201` | Link accepted; job queued. The document's `filename` is provisional until the download finished. |
| `400` | Link refused: scheme not allowed, credentials in the URL, private/internal address, host does not resolve, bucket not allowed, S3 not configured, malformed. `detail` explains. |
| `401` / `403` | Not signed in / missing `documents:write`. |
| `422` | Body is not `{"url": "<string up to 2048 chars>"}`. |
| `503` | Broker unavailable; nothing was queued. |

Errors are `{"detail": "<message>"}` like every other endpoint. Failures **after** the `201` (download refused or
failed, wrong file type, too large) appear on the job: `status: "failed"`, `error: "..."`.

* **The download runs in the worker.** The API only validates the link and records the job. The full URL is passed to
  the worker in the broker message only; the database, API responses, logs and traces hold the URL with credentials
  and query string removed (a presigned URL's signature is in its query).
* **Submitting the same link twice creates two documents** (no de-duplication by URL; delete one if unwanted).
* **SSRF protection** (`app/services/urlguard.py`): scheme allow-list; no `user:pass@`; the host is parsed by the app
  itself (decimal / hex / octal / short IPv4, IPv6, IPv4-mapped, NAT64, 6to4, IDNA) and a hostname is resolved by the
  app: **every** returned address must be public (a name with one public and one private record is refused). The
  blocked ranges are loopback, RFC 1918, link-local (incl. `169.254.169.254`), CGNAT `100.64/10`, `0/8`, multicast,
  reserved, benchmarking/documentation ranges, IPv6 loopback/ULA/link-local/site-local/multicast/Teredo, and the Azure
  wireserver; names `localhost`, `*.localhost`, `*.local`, `*.internal`, `metadata*` are refused up front. The connection
  is made to the **validated IP** (Host header and TLS SNI/certificate check keep the hostname), which defeats DNS
  rebinding. Redirects are followed by the app (max `URL_INGEST_MAX_REDIRECTS`) and **each hop** goes through the same
  checks, including a redirect to `s3://` or `file://`. Proxy environment variables are ignored.
* **Download limits**: the cap is applied to the bytes received (decoded), not to `Content-Length`; connect/read
  timeouts plus an overall deadline.
* **File type**: the extension (from `Content-Disposition`, else the final URL's path) must be `pdf`/`docx`/`txt`/`md`;
  when the name has none, the content type decides (`application/pdf`, `text/plain`, `text/markdown`, DOCX). HTML
  responses are refused; PDF needs a `%PDF-` header, DOCX a zip header, text must not contain NUL bytes. The file name is
  sanitised (no directories, traversal, odd characters).
* **`s3://bucket/key`**: read with the server's S3 credentials/endpoint, only from `URL_INGEST_S3_BUCKETS` (default
  `S3_BUCKET`), size-capped like an HTTP download, and **copied** into the configured storage so the document owns its
  copy (deleting the document never touches the source object).
