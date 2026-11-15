# Incremental document update (design)

Status: **Implemented (historical design record)** — written before the chunker and ingest pipeline to pin the chunking strategy; the update path shipped with the document update endpoint (`PUT /documents/{id}`, `backend/app/services/incremental.py`). Current behaviour: [ADR-0003](../adr/0003-chunking.md) and [`configuration.md`](../configuration.md).

This doc captures the brainstorming that led to the CDC decision in
[`ADR-0003`](../adr/0003-chunking.md). Read this when building the chunker and the
ingest pipeline; the update path itself is a follow-up ticket after 6.

## Problem

A user edits a document and re-uploads it. We want to refresh its embeddings **without
re-embedding the whole document** — only the chunks that actually changed. That requires
answering, for each chunk: *same as before, new, or deleted?* — and doing it with a cheap
comparison (a content hash), not a re-embed.

## Why this is unreliable for naive chunking

If we chunk by fixed-size windows from an absolute offset, then **one inserted character at the
top of the document shifts every subsequent chunk boundary**. Every chunk gets a different hash
and the whole document gets re-embedded — the feature buys nothing.

This is the "slightly offset" failure: boundaries drift, hashes miss, spurious updates fire.

## What makes it reliable

**Content-defined chunking (CDC).** Boundaries are decided by a rolling hash over a *local*
window (`hash(window) & mask == 0`), so they are a pure function of the text. An unchanged
paragraph re-chunks identically regardless of what changed elsewhere:

- edit inside a chunk → only chunks in the edit's neighborhood get new content/hashes
- chunks far from the edit keep identical text → identical hashes → reused
- deleted text → its chunks drop out of the new set (removed)
- inserted text → new chunks appear in the new set (added)

Only `added` chunks are embedded and upserted; `removed` chunks are deleted by `doc_id`; `kept`
chunks are untouched (no re-embed, no re-write).

Reliable **only if** the chunker is deterministic and content-addressed. Anchor/paragraph
splitting is a weaker approximation and additionally **fails when the parser is Tesseract OCR**,
which emits a flat text stream with no usable paragraph/section anchors (vision LLMs can emit
structured markdown; traditional OCR cannot). That double failure is why we chose CDC.

## Pipeline

On a re-ingest of an existing document (`job_id` for the doc):

1. Parse → chunk with the **same chunker config**.
2. Compute the per-chunk content hash (SHA-256 over normalized text — NFC, whitespace-collapsed —
   so parse-output drift doesn't create phantom diffs).
3. Load the document's previous chunk hashes (stored in `Chunk.metadata` / vector-store
   metadata).
4. `kept = old ∩ new`, `added = new − old`, `removed = old − new` (align by content hash).
5. Embed `added` only; `ChromaStore` upserts added + drops removed (we already have
   `did_similarity_search`-safe `delete_doc` / ID-based upsert primitives).
6. Bump the document's revision; record `added`/`removed`/`kept` counts on the Job.

## Reliability guards

- **Chunker fingerprint.** Store strategy + `chunk_size` + `chunk_overlap` + rolling-hash seed on
  the `Document`/`Job`. If it changes between ingests, the hash ledger is **invalid** — fall back
  to full re-chunk + diff (or full rebuild), never trust stale hashes.
- **No churn-based fallback by default.** The original design fell back to delete-doc + re-embed
  when `len(added) + len(removed) > ~half the doc's chunks`. A real revision of the Fannie Mae
  Selling Guide showed why that is wrong: with 365 old chunks, 169 reusable, 181 new
  and 196 removed, the rule rebuilt all 350 chunks while incremental embeds only the 181 added. An
  incremental update never embeds more than a rebuild, so it is the default for any amount of churn.
  `INCREMENTAL_MAX_CHURN` (unset by default) re-enables a rebuild guard if a deployment wants one.
- **OCR caveat.** If parse output is inherently noisy (Tesseract), chunk hashes still work (CDC is
  content-based) but *content stability* between runs is lower — normalization matters, and the
  embedding-similarity fallback (cosine vs. the old embedding, reuse if > ε) is the optional
  escape hatch for near-miss boundaries.

## Where it lives

- **Chunker**: `app/services/chunking.py` — `chunk_text(text, *, chunk_size,
  chunk_overlap) -> list[ChunkSpec]`, `ChunkSpec` gains `content_hash`, chunker fingerprint
  helper.
- **Ingest**: `app/tasks/ingest.py::process_document` orchestrates parse → chunk →
  diff → embed-added → upsert/drop → job/status update.
- **KB/vector store** (done): `ChromaStore.add_chunks` / `delete_doc` / metadata with
  `doc_id` + `index` — plus `content_hash` metadata added by the chunker.
- **DB** (done): `Chunk.metadata` JSON holds `content_hash` + `chunker_fingerprint`;
  the reserved `Chunk.vector(1024)` column stays unused (pgvector co-retrieval is a later,
  separate decision).

## Out of scope

- pgvector co-location of vectors.
- Token-aware (LLM-tokenizer) splitting — CDC operates on text characters/tokens of our choosing;
  a future token-aware max-size cap is possible but not required.
- Cross-document dedup (same chunk in two docs) — out of scope; hashing is per-document.