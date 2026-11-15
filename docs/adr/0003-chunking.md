# 0003 — Chunking strategy: content-defined chunking (CDC)

- Status: **Accepted**
- Date: 2026-09-09
- Status note: decision made before the chunking service was implemented, to pin the
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
(`backend/app/services/chunking.py`):

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
- Ingest runs the chunked text through the incremental-update path; the chunking
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

## Update 2026-11-14 — word-boundary cuts and neighbour context

**Problem (measured on the real Fannie Mae Selling Guide chapters).** The v1 chunker cut at
arbitrary characters: 303 of 335 chunks of the Nov 5 B3-5 chapter started or ended mid-word
("nder must confirm...", "...bankruptcy actio"), the average chunk was 281 chars, and rules were
split across chunks (two labelled questions failed because the completing sentence sat in the
neighbouring chunk).

**Algorithm (`cdc-buzhash-v2-words`).** Still content-defined, still a pure function of the text:

- Candidate cuts exist only at *word starts* (previous char is whitespace, current is not), so a
  chunk never begins or ends mid-word (a whitespace-free run longer than `chunk_size` is the one
  exception and is hard-cut).
- Past the minimum length (`CHUNK_OVERLAP`) a candidate fires when a rolling hash of the preceding
  32 *non-whitespace* characters hits a mask. Sentence ends (`. ! ? : ;`, optionally followed by
  closing quotes/brackets) and blank lines use a loose mask, mid-sentence word starts a much
  tighter one, so most cuts land on sentence ends (55-65% on the real chapter) with a word-boundary
  fallback for long sentences. A lone newline is not a sentence end (PDF text wraps lines).
- The hash skips whitespace, so re-wrapped lines or changed spacing between two renderings of the
  same document do not move cut points.
- A chunk reaching `CHUNK_SIZE` is cut at its last candidate (so it is still on a word boundary).
- Rejected: local-extrema chunking (a cut = the highest-ranked candidate within +/-min chars).
  It has no dependence on the previous cut, but a cut then depends on text up to `min` chars away,
  so each edit disturbs more cuts; measured offline on the real update it re-cut 41-59 chunks at
  ~250-335 chunks total vs ~25 for the threshold form at similar chunk counts.

**Parameter choice (hold-out discipline).** Defaults moved from 512/64 to 1000/100 (average ~525
chars instead of ~280). Chosen on size and stability metrics (real B3-5 Oct 8 -> Nov 5 update) and
a *dev* half of the labelled questions (odd ids); the *hold-out* half (even ids) was only used to
confirm. Retrieval over 12 questions is very noisy: re-seeding the hash (same parameters, different
cut points) moves hit counts by +/-1-2, so differences between sizes of 640-1000 are within noise and
were not used to pick between them. Larger chunks cost more per edit (characters re-embedded after
the real update: 50% of the text at 512/64, ~74% at 1000/100, because the revision touches almost
every page) but need roughly half as many vectors, give a complete rule more often, and the
neighbour step (below) recovers the rest. Labels in `tmp/smoke_tests/search-questions.json` were
not edited.

**Neighbour context.** `RETRIEVAL_NEIGHBORS=N` (`services/context.py`) is a post-retrieval step
independent of how hits were found (it only needs `(document, chunk_index)`): it adds chunks
`i-N..i+N` of every hit from the same document, de-duplicated, in reading order, within the prompt
context budget (nearest neighbours first; hits are never dropped). Default `1`; see the smoke
report for the numbers behind that default.

**Consequences.** `ALGORITHM` changed, so every stored chunk's fingerprint mismatches: each document
is fully re-chunked and re-embedded on its next ingest ("chunker config changed"); until then old
and new chunks are not mixed within a document. Boundary resynchronisation after an edit did *not*
improve materially (see smoke report): most "re-cut only" chunks come from the edit's own
neighbourhood and from the revision touching nearly every page, not from slow resync.
