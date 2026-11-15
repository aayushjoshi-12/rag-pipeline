# Configuration reference

Settings are layered (highest wins): **environment variables (including a `.env` file) > the admin
Settings window (database) > `config/config.<RAG_ENV>.json` > built-in defaults**
(`backend/app/config.py`, [ADR-0008](adr/0008-runtime-settings.md)). See
[The Settings window](#the-settings-window-admin-ui) for the UI layer. Every choice is a flat key with a validated range; an invalid value
fails at startup with the allowed values. **Scope** says when a change takes effect:

- *query-time* — applies to the next request; may also be overridden per request.
- *index-time* — changes how documents are indexed; existing documents are re-chunked and
  re-embedded on their next ingest (the chunker fingerprint no longer matches).
- *reindex* — changes how the **vector store** is built (store, metric, index type/params,
  embedding dimension). The app detects the difference at startup and refuses to search a
  mismatched index; fix it with `python -m app.manage reindex` (see
  [Changing the vector store](#changing-the-vector-store)).

## Providers at a glance

Everything is swappable by configuration: `LLM_PROVIDER` (openrouter | openai | anthropic), `EMBEDDING_PROVIDER` (ollama | openai) and
`PARSER_PROVIDER` (openrouter | openai | anthropic | ocr). Free-tier OpenRouter models come and go: if chat returns a 502, check
`DEFAULT_LLM_MODEL` against OpenRouter's current model list. `VECTOR_STORE` (chroma | pgvector), `VECTOR_METRIC`, `VECTOR_INDEX`
(hnsw | ivfflat | none) and the HNSW / IVFFlat knobs choose where vectors live and how they are indexed; changing them on existing data is
a `python -m app.manage reindex`. `RETRIEVAL_MODE` (semantic | keyword | hybrid, also per request on `POST /chat`), `RETRIEVAL_KEYWORD_METHOD`,
`RETRIEVAL_FUSION` and the BM25 / RRF / weight knobs choose how chunks are found; `semantic` is the default. The table below lists them all.

| Key | Values | Default | Scope | Meaning |
|---|---|---|---|---|
| `CHUNK_SIZE` | integer > 0 (chars) | `1000` | index-time | Hard **maximum** chunk length. Chunks average roughly 0.5 x this (~525 chars at 1000) and are cut at word starts, preferably at sentence ends / blank lines (ADR-0003). |
| `CHUNK_OVERLAP` | integer, `0 <= v < CHUNK_SIZE` (chars) | `100` | index-time | **Minimum** chunk length (nothing is repeated between chunks; the name is kept for compatibility). A smaller minimum resynchronises cut points faster after an edit but allows smaller chunks. |
| `RETRIEVAL_NEIGHBORS` | integer `0..5` | `1` | query-time (per-request `neighbors` on `POST /chat`) | Besides each search hit, also give the LLM the N chunks before and after it from the same document (de-duplicated, in reading order, bounded by the 12,000-char prompt context budget; hits always keep priority). `0` = hits only. Response `sources[].is_hit` is `false` for context-only chunks. |
| `DEFAULT_TOP_K` | integer >= 1 | `5` | query-time (per-request `top_k`) | Number of search hits (before neighbour expansion). |
| `RETRIEVAL_MODE` | `semantic`, `keyword`, `hybrid` | `semantic` | query-time (per-request `mode` on `POST /chat`) | How chunks are found. `semantic` = embeddings only (the original behaviour). `keyword` = Postgres full-text search with BM25 re-scoring, no embedding call. `hybrid` = both, fused. See [Retrieval modes](#retrieval-modes). |
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
| `PARSE_NORMALIZE_TEXT` | `true`, `false` | `true` | index-time | Fold PDF/Word/text extraction quirks before chunking: Latin ligatures (`ﬁ ﬂ ﬀ ﬃ ﬄ` -> `fi fl ff ffi ffl`), no-break/thin/ideographic spaces -> space, soft hyphen and zero-width characters removed, curly quotes -> `'` `"`, canonical (NFC) composition. Deliberately **not** NFKC: superscripts, fractions, `™`, full-width forms, CJK and dashes are left as extracted. See [Text normalisation](#text-normalisation). |
| `STORAGE_BACKEND` | `local`, `s3` | `local` | deployment (per document: `documents.storage_backend`) | Where uploaded / downloaded source files are kept. `local` = `DATA_DIR/uploads/<id>/<name>`. `s3` = any S3-compatible store through boto3. Existing documents stay where they were written. See [File storage and URL ingest](#file-storage-and-url-ingest). |
| `S3_ENDPOINT_URL` | http(s) URL or empty | empty (= AWS) | deployment | Endpoint of an S3-compatible store (Floci: `http://floci:4566`). Path-style addressing is used when set. |
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
| `WORKER_CONCURRENCY` | integer `1..64` | `2` | deployment (worker restart) | Celery prefork children per worker. Celery's own default is one per host CPU, which is the *node's* CPU count inside a K8s pod whatever its CPU limit; each child holds ~100 MiB private memory, so 10 children idled at ~1.1 GiB (2 children: ~445 MiB). Ingestion mostly waits on embedding/vision HTTP calls, so add worker replicas rather than children. See [Memory](#memory-and-limits). |

Why these defaults: see "Parameter choice" in `docs/adr/0003-chunking.md`.

Settings not tabulated here: the parser/OCR knobs (`PARSER_*`, `PDF_DPI`, `PDF_TEXT_SOURCE`, `PDF_NATIVE_MIN_CHARS`, `OCR_LANG`, `DOCX_READER`, `OPENROUTER_BASE_URL`) are described in [ADR-0002](adr/0002-document-parsing.md); provider keys, `OLLAMA_BASE_URL`, `CHROMA_HOST`/`CHROMA_PORT` (default `localhost`/`8001`), `CELERY_*`, `PORT` and `LANGFUSE_*` are in `.env.example`. Also real settings: `PARSER_MAX_COMPLETION_TOKENS` (default 16032, range 256..32000, output cap of one vision-parse call), `INCREMENTAL_MAX_CHURN` (unset; optional rebuild guard, see [the incremental-update design](design/incremental-document-update.md)), `ACCESS_TOKEN_MINUTES` (30) and `REFRESH_TOKEN_DAYS` (7) for the login token lifetimes.

## The Settings window (admin UI)

Admin > **Settings** (permissions `settings:read` to view, `settings:manage` to change) edits almost every
setting without touching files. It adds one layer between the environment and the config file:

| Layer (highest first) | Where it lives | Who changes it |
|---|---|---|
| 1. environment variable / `.env` | process environment, K8s Secret/ConfigMap | operator |
| 2. **UI (database)** | `app_settings` table | admin, in the Settings window |
| 3. `config/config.<RAG_ENV>.json` | image | developer |
| 4. built-in default | code | developer |

- A setting pinned by layer 1 is shown **locked** with the reason (`set by environment variable X`) and
  cannot be edited; a UI value stored earlier is kept but ignored (shown as "shadowed"). **To manage a
  setting from the UI, remove it from the environment.** Note that Compose passes everything in `.env`
  and several keys in `docker-compose.yml` (e.g. `S3_*`, `STORAGE_BACKEND`, `PARSE_NORMALIZE_TEXT`) into the
  containers, and that an *empty* variable still counts as set: delete the line to unpin the key.
- Each setting says when a change applies: **live** (next request / next Celery task, no restart),
  **needs restart** (stored now, used after the API restarts: `CORS_ALLOW_ORIGINS`, `LOG_LEVEL`,
  `LANGFUSE_*`), **needs reindex** (embedding provider/model/dimension, vector store/host/collection,
  metric, index type, HNSW M/ef_construction, IVFFlat lists, FTS configuration: the page asks for a
  confirmation, applies the value and queues the reindex; progress is shown), or **read-only**
  (bootstrap values such as `DATABASE_URL`, `CELERY_*`, `SECRET_KEY`, `AUTH_ENABLED`,
  `URL_INGEST_ALLOW_PRIVATE`, `TESSERACT_CMD`, `DATA_DIR`, which must exist before the database can be read or are too
  dangerous to change from a web page).
- `CHUNK_SIZE`/`CHUNK_OVERLAP`/`PARSE_NORMALIZE_TEXT` are live but apply to documents ingested afterwards
  (re-upload to re-chunk); a reindex re-embeds the stored chunk text, it does not re-chunk.
- **Services** at the top guide a provider switch (chat model, embeddings, vector store, file storage,
  parser): the fields that belong together, the consequence, and **Test connection** (key/models check,
  one tiny embedding, heartbeat, head-bucket; the saved settings are tested, never a secret returned).
- **Secrets are write-only.** API keys etc. are stored encrypted (Fernet, key derived from
  `SETTINGS_ENCRYPTION_KEY`), never returned by any endpoint or shown again (only "set" / "not set" and who
  changed it), and can be replaced or cleared. Without `SETTINGS_ENCRYPTION_KEY` (a random string of 32+
  characters in the server environment) secret fields are disabled and everything else still works.
  Rotation: put the new key in `SETTINGS_ENCRYPTION_KEY`, keep the old one in
  `SETTINGS_ENCRYPTION_KEY_PREVIOUS`, run `python -m app.manage rotate-settings-key`, then drop the old one.
- Replicas and workers read the layer from the database: the API re-reads it every
  `SETTINGS_REFRESH_SECONDS` (default 5) and right after its own writes; a Celery task reads it when it
  starts. If the stored values ever form an invalid combination they are not applied (the last good values
  stay) and the page says so.
- Every change is recorded in the audit log (who, when, old/new value; **never a secret value**).

| Key | Values | Default | Meaning |
|---|---|---|---|
| `SETTINGS_ENCRYPTION_KEY` | string, >= 32 chars | empty | Encrypts secrets stored from the UI (env / K8s Secret only). |
| `SETTINGS_ENCRYPTION_KEY_PREVIOUS` | comma separated strings | empty | Old key(s) still accepted for decryption while rotating. |
| `SETTINGS_REFRESH_SECONDS` | float `0.5..3600` | `5` | How often each process re-reads the UI layer. |

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
* **Local S3 emulator (Floci)**: `docker compose --profile s3 up -d floci floci-init` starts [Floci](https://floci.io), a
  free MIT-licensed AWS emulator (~40 MiB idle, S3 on `:4566`, any credentials accepted), and creates the bucket
  `$S3_BUCKET` (default `rag-documents`) with `python -m app.manage ensure-bucket`. The default `docker compose up` does
  not start it. Containers reach it as `http://floci:4566`: set `S3_ENDPOINT_URL_DOCKER=http://floci:4566` (and
  `S3_ENDPOINT_URL=http://localhost:4566` for host-run tools/tests), with any `S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY`
  (e.g. `test`). `FLOCI_IMAGE` overrides the image. It replaced MinIO, which no longer publishes a maintained image.
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

## Text normalisation

Born-digital PDFs store typographic ligatures as single characters: the Selling Guide text layer says `conﬁrm`
(U+FB01), so a BM25 / full-text query `confirm` never matches that chunk and the embedder sees a different token.
`PARSE_NORMALIZE_TEXT=true` (default) folds this in the parse seam (`app/services/parser.py` -> `app/services/textnorm.py`),
after extraction and before chunking, for every file type.

**Decision: a targeted map + NFC, not NFKC.** NFKC fixes the ligatures but also rewrites characters whose meaning matters:
`m²` -> `m2`, footnote `word¹` -> `word1`, `½` -> `1⁄2` (digit + U+2044, which nobody types), `™` -> `TM`, `№` -> `No`,
`…` -> `...`, the micro sign `µ` -> Greek `μ`, `㎏` -> `kg`, `①` -> `1`, `Ⅳ` -> `IV`, and full-width/half-width forms
(`Ａ１`, `ｱ`) -> other characters, i.e. it edits the text of CJK documents. It also does not touch curly quotes or no-break
spaces, which hurt matching too. The map folds only: Latin ligatures U+FB00..FB06; no-break / narrow no-break / thin / hair /
figure / ideographic spaces -> one space; soft hyphen and zero-width space/joiner/non-joiner/word joiner/BOM -> removed;
`‘ ’ ‚ ‛ ′` -> `'` and `“ ” „ ‟` -> `"`; then NFC (lossless canonical composition, `e` + combining acute -> `é`).
Test data for what must not change is in `tests/unit/test_textnorm.py`.

**Upgrading.** The chunker fingerprint is unchanged (the chunking *algorithm* is the same), so nothing is forced: existing
documents keep their old chunks (with ligatures) until they are ingested again. Re-ingest to apply it: `PUT /documents/{id}`
with the same file, or re-run the job. That re-ingest is **incremental**: only the chunks whose text contained a folded
character get new content hashes and are re-embedded; unchanged chunks are kept (a test pins this). `python -m app.manage reindex`
rebuilds the *vector store* from the stored chunk text and therefore does **not** remove ligatures that are already in it.
Set `PARSE_NORMALIZE_TEXT=false` to keep the text exactly as extracted (index-time: affects documents ingested afterwards).

## `POST /search` and neighbour context

`POST /search` ([docs/api.md](api.md)) takes the same query-time knobs as `/chat` (`mode`, `fusion`, `top_k`,
`document_ids`, `neighbors`) and has no settings of its own. Differences worth knowing:

- `neighbors` defaults to **0** on `/search` (it does not follow `RETRIEVAL_NEIGHBORS`, which shapes the `/chat`
  prompt). The neighbour budget (12,000 characters) is shared with `/chat`.
- Neighbour (context) chunks are unscored. In `/search` they are listed under their hit as `results[].context`; in
  `/chat` they stay in `sources` (prompt order) with `score: null`, no ranks and `context_for` naming their hit.
- Permission `search:use` (built-in `admin`, `user`, `viewer`); existing installs get it from migration `0007`.
- Each call is traced as kind `search` (`meta`: `mode`, `fusion` (hybrid), `top_k`, `neighbors`, `document_filter`,
  `include_text`; the query only when `TRACE_CAPTURE_CONTENT=true`) and shows in Traces and Usage under type
  "Search".
## LLM cost and the Usage page

Each chat trace stores `cost_usd` (USD). **Unknown is `NULL`, never 0**: the dashboard shows "—" for it.

| Key | Values | Default | Scope | Meaning |
|---|---|---|---|---|
| `LLM_PRICE_INPUT_PER_MTOK` | float `>= 0` | unset | deployment | USD per million prompt tokens. Used for providers that do not report a cost themselves (`openai`, `anthropic`). Must be set together with the output price; unset = cost unknown. |
| `LLM_PRICE_OUTPUT_PER_MTOK` | float `>= 0` | unset | deployment | USD per million completion tokens (see above). |

- `LLM_PROVIDER=openrouter` needs no price settings: requests carry `usage: {include: true}` and OpenRouter returns the real cost in `usage.cost` (0 for `:free` models). The flag is sent only to OpenRouter.
- A provider-reported cost always wins over the configured prices.
- Usage is kept on **failed** calls too: a reasoning model that spends its whole output budget thinking is still billed, so the error trace carries the provider's tokens and cost. A client that disconnects mid-stream never receives the final usage chunk, so those traces have none.
- `GET /usage/credits` (needs `traces:read`) reads the OpenRouter key balance (`GET /key`) server-side, caches it 60 s, and returns `available: false` with a reason for other providers, a missing key or an unreachable endpoint. The API key is never returned.
- `GET /usage/insights?days=&kind=` returns the aggregates behind the Usage tabs (cost by model, latency histogram, error reasons, weekday/hour heatmap, no-hit rate, hits per query, most-cited documents, chunk reuse, ingest duration by mode). Chat traces store the ids (never the text) of the documents an answer was grounded on in `meta.cited_document_ids`.

### Output budget (`LLM_MAX_TOKENS`) and embedding usage

| Key | Values | Default | Scope | Meaning |
|---|---|---|---|---|
| `LLM_MAX_TOKENS` | int `1..200000` | `2048` | deployment (env or `config.json`) | Maximum output tokens of one answer, sent to every LLM provider. Reasoning models count their hidden thinking against it: too small and the reply is empty. The error then says "LLM ran out of output tokens (finish_reason=length, max_tokens=N)"; raise this value or use a non-reasoning model. |
| `EMBEDDING_PRICE_PER_MTOK` | float `>= 0` | unset | deployment | USD per million embedded tokens for a **hosted** embedding provider (`EMBEDDING_PROVIDER=openai`). Local Ollama is always `$0`. Unset on a hosted provider = embedding cost unknown (`n/a` in Traces). |

- Embedding tokens are what the provider reported (Ollama `prompt_eval_count`, OpenAI `usage.total_tokens`); nothing is estimated. A `search` trace records the query embedding (`prompt_tokens`, `completion_tokens = 0`, `meta.embedding.tokens`); a cached embedding or retrieval is `0` with `meta.embedding.cached = true`. A `chat` trace keeps the LLM's tokens and puts the query embedding in `meta.embedding` only. An `ingest` trace records the tokens of the chunks it embedded (`meta.embedding_tokens`; `0` when an incremental update re-embedded nothing). Keyword search embeds nothing and records none.
- Traces table: a number is what the provider reported, `cached` means the cache answered (nothing was spent), `n/a` means a successful call without a reported count/price (old traces too), and `—` means not applicable (failed without usage, keyword search).


## API keys (service accounts)

API keys need **no new settings**: they are stored in Postgres (`api_keys`, migration `0009`) and are available whenever
`AUTH_ENABLED=true`. They are managed at runtime (dashboard Admin > API keys, or `/api-keys`), not through config.

| Aspect | Value | Scope | Meaning |
|---|---|---|---|
| Permission `apikeys:manage` | new; built-in `admin` only | deployment (RBAC) | create / list / revoke keys. Add it to a custom role in Admin > Roles if a non-admin should manage keys. |
| Key format | `rag_<8 [a-z0-9]>_<256-bit urlsafe secret>` | fixed | not configurable |
| `last_used_at` write interval | 60 s per key | fixed (code constant) | bounds database writes from busy clients |
| Max `expires_in_days` | 3650 | fixed | omit for no expiry |
| Per-key rate limit | not implemented | n/a | rate-limit at the gateway/ingress |

See [`docs/api.md`](./api.md#authentication-for-services-api-keys) for the security model and a curl/Python recipe.

## CORS

| Key | Values | Default | Scope | Meaning |
|---|---|---|---|---|
| `CORS_ALLOW_ORIGINS` | comma separated origins (`https://app.example.com`, scheme + host[:port], no path) or `*` | empty | startup (restart) | Browser origins allowed to call the API. Empty = no CORS headers at all (same-origin and non-browser clients only). An invalid entry fails at startup. |

Credentials are never allowed cross-origin (auth is by header), and the allowed methods/headers are fixed; see
[`docs/api.md`](./api.md#cors-browser-clients). The dashboard container proxies `/api`-style calls itself, so it does not need this.

## Memory and limits

Measured on a 7.7 GiB Docker VM (raw numbers in `tmp/smoke_tests/48-49-memory-result.md`). The VM
kernel log had shown global OOM kills of Chroma (5.7 GB and 5.5 GB resident) and of the api (3.3 GB): with no memory
limit the kernel picks whichever process is biggest, not the one that ran away.

| Service | Idle | What grows it | Compose | K8s request / limit |
|---|---|---|---|---|
| chroma | ~70 MiB (800 vectors) | vectors (~4x their raw float32 size, see rule), **every collection ever written to, even after it is deleted** | `mem_limit 1536m` | 512Mi / 1Gi |
| api | ~160 MiB | uploads stream to disk in 1 MiB chunks (`MAX_UPLOAD_SIZE_BYTES`); ~275 MiB after three 200-page ingests | `mem_limit 1g` | 256Mi / 1Gi (prod 2Gi) |
| worker | ~445 MiB at `WORKER_CONCURRENCY=2` (was ~1.1 GiB with Celery's default of one child per host CPU) | one ingest per child; after three 200-page text-PDF ingests (1827 chunks each) ~570 MiB (re-measured: 20 small ingest+delete cycles move the worker 357 -> 369 MiB and the api 195 -> 196 MiB anon, i.e. no leak; six 500 KB / ~600-chunk documents: worker 473 -> 550 anon, api 197 -> 215, still drifting up slowly: probably allocator high-water, not proven either way), a 200-page *scanned* PDF ~280 MiB per process, a DOCX +45 MiB | `mem_limit 3g` | 512Mi / 3Gi (prod 4Gi) |
| beat | ~176 MiB (cold-start peak ~180 MiB; it imports every task module through `include=`) | nothing: a tick only publishes a message | `mem_limit 384m` | 192Mi / 384Mi |

* **Chroma sizing rule.** `memory ~= 150 MiB + 16 bytes x embedding dimension x vectors`. 512Mi/1Gi therefore holds about
  40k vectors of 1024 dimensions; double the limit for every doubling. Vectors in Postgres are not part of this.
* **Chroma leaks on delete (upstream, chroma 1.4.4 server, image `chromadb/chroma:1.5.9`).** A collection that was
  written to keeps its HNSW index in memory after `delete_collection`, and deleting/re-adding vectors inside one
  collection keeps the old graph too (small collections: ~3 MiB each with the default config, ~0.15 MiB with the
  bounded cache below; measured 2768 x 1024 vectors: ~15 MiB per drop-and-recreate reindex). Therefore:
  `reindex` no longer drops a Chroma collection when nothing drifted: it upserts every vector and deletes the ids that
  are no longer chunks (measured **15.0 -> 0.5 MiB per reindex**, though a rewrite in place takes longer, ~12 s vs ~3 s
  for 2768 vectors). It still drops and recreates when `verify()` reports drift (metric, HNSW parameters, dimension),
  which is the only way to apply new index parameters, and that path still leaves one index behind: restart Chroma
  afterwards (`docker compose restart chroma`, `kubectl -n rag-pipeline rollout restart deploy/chroma`; the data is on
  the volume). The Compose and K8s Chroma mount `chroma/config.yaml` (`hnsw_index_pool_cache_config.lru.capacity: 16`).
  With the memory limit set, the worst case is that Chroma alone is OOM-killed and restarted.
* **The image tag is pinned** (`chromadb/chroma:1.5.9` in Compose, `k8s/base/chroma`, CI; recorded in
  `chroma/config.yaml`) because this behaviour is version dependent. Bump all places together and re-measure.
* **Tests never touch the dev Chroma**: `make check` runs the Chroma tests on a throwaway `chroma-test`
  container (:8002, tmpfs). A dev Chroma that was used by older test runs holds thousands of orphan HNSW segment
  directories on its volume (6928 / 260 MB seen) and keeps their indexes in memory. One-off cleanup, only after
  `docker compose stop chroma api worker`: back up nothing (vectors are rebuildable), `docker volume rm rag-pipeline_chroma-data`,
  `docker compose up -d chroma api worker`, then `docker compose exec worker python -m app.manage reindex` (re-embeds the
  stored chunk text; no re-parsing). Stale `test_*` collections alone can be dropped with the Chroma client
  (`client.delete_collection("test_store")`).
* **Scanned PDFs.** Pages that need OCR/vision are rendered one at a time (`render_page`) and every pdfium page and
  bitmap is closed right after it is read. Before, all pages were rendered up front: at the default `PDF_DPI=300` a 200-page scan needs
  ~25 MB per page, about 4.4 GB, and was OOM-killed. `MAX_UPLOAD_SIZE_BYTES` (25 MiB) still bounds the file itself.
* **Worker memory vs `WORKER_CONCURRENCY`.** Budget `~220 MiB + ~110 MiB x WORKER_CONCURRENCY` per worker, plus the
  peak of the documents in flight; set the request to the idle figure and the limit to idle + one large document per child.

## Query cache

Three layers in the existing Redis (own db, keys under `rag:cache:v1:`), all **fail-open**: with Redis down, slow or full
a request is served uncached, never failed. Implemented in `app/services/cache.py`; benchmark in [`performance.md`](./performance.md).

| Key | Default | Meaning |
|---|---|---|
| `CACHE_ENABLED` | `true` | Master switch. `false` = `X-Cache: off`, no Redis calls. |
| `CACHE_REDIS_URL` | `redis://localhost:6379/12` | Must not equal the Celery broker/result URL (startup error): a purge never touches Celery keys. Compose/K8s set `redis://redis:6379/12`. |
| `CACHE_EMBEDDING_TTL_SECONDS` | `86400` | L1: query embedding (saves the Ollama/OpenAI call). `0` = layer off. |
| `CACHE_RETRIEVAL_TTL_SECONDS` | `600` | L2: ranked passages for `/search` and `/chat`. `0` = off. |
| `CACHE_ANSWER_TTL_SECONDS` | `3600` | L3: the `/chat` answer (saves the LLM call and its cost). `0` = off. |
| `CACHE_MAX_VALUE_BYTES` | `262144` | Larger values are not stored. |
| `CACHE_TIMEOUT_MS` | `100` | Per Redis operation; slower = treated as down. |
| `CACHE_BREAKER_SECONDS` | `5` | After a failure the cache is skipped this long, so an outage adds no per-request latency. |

- **Keys.** L1: embedding model + query (NFC, trimmed, whitespace collapsed; **case is preserved**, embedding models are case sensitive). L2: query,
  `top_k`, neighbours, sorted document filter, only the retrieval options that matter for the mode, a fingerprint of the vector-store /
  embedding / chunking settings and the **index version**. L3: question + hash of the *exact* prompt including the retrieved context + model + system prompt.
  An answer is therefore never reused for a different context. Keys contain no user identity: the corpus is shared, and authorization always runs
  before any cache lookup. If per-user document ownership is ever added, add the owner/ACL scope to the L2/L3 keys.
- **Invalidation.** A random *index version* token in Redis is replaced after every change to what is searchable: ingest finish, delete, replace, reindex,
  `rebuild-fts`, `rebuild-index`, and `POST /cache/purge`. Old entries become unreachable and expire by TTL. If Redis cannot be reached at that moment the
  process remembers and purges when Redis returns; **a different process** that failed to bump relies on the TTL (default 10 minutes) or a manual purge.
- **Bypass.** Body field `"cache": false` or request header `Cache-Control: no-cache`/`no-store`: no read, no write, `X-Cache: bypass`.
- **Visibility.** Responses carry `X-Cache: hit|miss|bypass|off` (+ `X-Cache-Layers`), `/search` and `/chat` bodies a `cache` field, traces `meta.cache`.
  `GET /cache/stats` (`traces:read`) returns live counters, Redis keyspace and trace-derived hit rates; `POST /cache/purge` needs the new permission
  `cache:manage` (built-in `admin`, re-synced automatically). The dashboard shows it under Usage > Cache.
- **Single flight.** Identical concurrent misses in one process compute once; followers wait. Across replicas it is one compute per replica.
- **Not built:** a semantic (near-duplicate) cache. It trades correctness for hits and was left out; exact-after-normalisation repeats only.
- Never cached: errors, empty answers, "no context" refusals.

