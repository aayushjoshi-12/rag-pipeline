# 0003 — Chunking strategy: content-defined chunking (CDC)

- Status: **Accepted**
- Date: 2026-09-09
- Status note: decision made while ticket 5 (chunking service) was still `open`, to pin the
  strategy before implementation.

## Context

The chunker splits parsed text/markdown into chunks for embedding. Two requirements shape the
choice:

1. **Parse backends produce inputs with unreliable structure.** Parsing (ADR-0002) is
   provider-switchable: vision LLMs (OpenRouter/OpenAI/Anthropic) return structured markdown with
   real paragraph/heading anchors, but **traditional OCR (Tesseract) returns a flat text stream
   with no reliable paragraph or section boundaries.** Any chunker that depends on structural
   anchors (paragraph-aware splitting) silently degrades when the parser is OCR because anchors
   barely exist in the input.

2. **Future incremental document updates need content-addressable chunks.** We want to re-ingest
   an edited document and re-embed *only the changed chunks* (see
   `docs/design/incremental-document-update.md`). That requires chunk boundaries (and thus chunk
   hashes) to be **stable under local edits**: editing one paragraph must not shift the boundaries
   of every later chunk. Position-based chunking (fixed-size windows from an absolute offset)
   fails this — one inserted character re-chunks the whole document.

## Decision

Adopt **content-defined chunking (CDC)** as the default strategy in the chunking service
(`backend/app/services/chunking.py`, ticket 5):

- Chunk boundaries are decided by a **rolling hash over a local window** (e.g. buzhash/fastcdc
  style), marking a boundary when `hash(window) & mask == 0`.
- Boundaries are therefore a **pure function of content**: unchanged text yields identical chunk
  boundaries (and identical chunks) no matter what changed elsewhere in the document.
- Enforce a **minimum chunk size** (avoid pathological tiny chunks at edge conditions) and a
  **maximum chunk size** (deterministically split an oversized run that never hits the mask).
- A **content hash** (SHA-256 over normalized text) is attached to every chunk so the ingest
  pipeline can diff an old vs. new chunk set without re-embedding.

Rejected alternatives:

- **Anchor / paragraph-aware splitting** — simpler, but fails when the parser is Tesseract OCR
  (context req. 1), and paragraph detection across providers is inconsistent.
- **Fixed-size / position-based windows** — simplest (existing `chunk_size=512,
  chunk_overlap=64` settings), but any edit shifts all downstream boundaries (context req. 2).
- **Embedding-similarity dedup** (embed everything, dedup by cosine) — no dedup cost saving; kept
  only as a fallback for near-miss hash comparisons, not as the primary mechanism.

## Consequences

- The chunker is deterministic (a pure function of input text + config) — required for both
  stability and testability.
- `ChunkSpec` gains a `content_hash` field (and retains stable `index` ordering).
- Ingest (ticket 6) runs the chunked text through the incremental-update path; the chunking
  ticket implements the primitive (`chunk_text` returning `ChunkSpec`s), the diff/align logic
  lands in the ingest ticket.
- Add a **chunker fingerprint** (strategy + `chunk_size` + `chunk_overlap` + hash seed) persisted
  per `Document`/`Job`: if the config changes, the hash ledger is invalid and the document is
  re-chunked from scratch before diffing.
- `chunk_size`/`chunk_overlap` remain as *target* bounds (max-size cap / min-size floor) rather
  than absolute window lengths, since CDC does not slice at fixed offsets.

## Future option (not decided)

Baking the rolling-hash chunker in as a vendored/self-contained module vs. adding a dependency
(e.g. `fastcdc`, `borg`'s algorithms) — keep it dependency-free with our own small buzhash
implementation unless correctness/performance testing says otherwise.