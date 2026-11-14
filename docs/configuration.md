# Configuration reference

Settings are layered: environment variables > `config/config.<RAG_ENV>.json` > defaults
(`backend/app/config.py`). Every choice is a flat key with a validated range; an invalid value
fails at startup with the allowed values. **Scope** says when a change takes effect:

- *query-time* — applies to the next request; may also be overridden per request.
- *index-time* — changes how documents are indexed; existing documents are re-chunked and
  re-embedded on their next ingest (the chunker fingerprint no longer matches).

| Key | Values | Default | Scope | Meaning |
|---|---|---|---|---|
| `CHUNK_SIZE` | integer > 0 (chars) | `1000` | index-time | Hard **maximum** chunk length. Chunks average roughly 0.5 x this (~525 chars at 1000) and are cut at word starts, preferably at sentence ends / blank lines (ADR-0003). |
| `CHUNK_OVERLAP` | integer, `0 <= v < CHUNK_SIZE` (chars) | `100` | index-time | **Minimum** chunk length (nothing is repeated between chunks; the name is kept for compatibility). A smaller minimum resynchronises cut points faster after an edit but allows smaller chunks. |
| `RETRIEVAL_NEIGHBORS` | integer `0..5` | `1` | query-time (per-request `neighbors` on `POST /chat`) | Besides each search hit, also give the LLM the N chunks before and after it from the same document (de-duplicated, in reading order, bounded by the 12,000-char prompt context budget; hits always keep priority). `0` = hits only. Response `sources[].is_hit` is `false` for context-only chunks. |
| `DEFAULT_TOP_K` | integer >= 1 | `5` | query-time (per-request `top_k`) | Number of search hits (before neighbour expansion). |

Why these defaults: see "Parameter choice" in `docs/adr/0003-chunking.md`.
