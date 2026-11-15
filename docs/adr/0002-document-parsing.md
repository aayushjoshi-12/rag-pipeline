# 0002 — Document parsing

- Status: **Accepted**
- Date: 2026-09-08 (rewritten 2026-11-13 as an original, self-contained design)

## Context

The RAG pipeline's ingest path must parse documents in the **Celery worker** (not the API). RAG
needs **text/markdown extraction** for chunking + embedding: no schema, no structured field
extraction, no multi-model reconciliation. Parsing must work on a laptop with no paid keys and
scale out unchanged, and must degrade sensibly across inputs (born-digital PDFs, scans,
handwriting, DOCX).

## Decision

The parser is original code in this repo (`backend/app/services/`), behind one seam:
`app/services/parser.py::parse(path, filetype, ...) -> ParseResult`.

| Piece | Module | What it does |
|---|---|---|
| PDF access | `services/pdf_pages.py` | Page count, native text layer per page, and one RGB image per page at the configured DPI (pypdfium2; one process-wide lock, handles closed explicitly) |
| Backend interface | `services/backends/base.py` | `BaseBackend`: `name`, `extract_text(image) -> str` |
| OCR backend | `services/backends/ocr_backend.py` | Tesseract on a page image (scanned PDFs); needs `tesseract-ocr` + `pytesseract` |
| OpenAI-compatible vision backend | `services/backends/openai_backend.py` | Chat-completions with an image part (OpenAI SDK) |
| OpenRouter backend | `services/backends/openrouter_backend.py` | OpenAI backend pointed at `OPENROUTER_BASE_URL` — our default provider |
| Anthropic vision backend | `services/backends/anthropic_backend.py` | Messages API with an image block |
| Error taxonomy | `services/parse_errors.py` | `DocumentLoadError`, `UnsupportedFileTypeError`, `InvalidBackendError`, `BackendCallError` (transient, retried), `BackendRequestError` (permanent) |
| Retry | `services/retry.py` | tenacity: 3 attempts, exponential backoff, transient errors only, applied per page |
| Tracing | `services/observability.py` | Langfuse generation spans around LLM calls (content capture opt-in) |
| Backend factory | `services/backends/__init__.py` | `get_parser_backend(provider, settings)`; missing keys raise, never silently fall back |

**DOCX → markdown via docling's Word backend** (tables rendered as markdown), not raw text. Only
`docling-slim[format-docx,format-pdf-pypdfium2]` is installed and the backend is called directly
(`MsWordDocumentBackend`): DOCX conversion is an XML → document transform and needs no OCR or
layout models. The full `docling` package (and `DocumentConverter`, which imports the OCR/layout
pipelines) would pull in torch and several GB for nothing.

Explored alternatives for DOCX: `mammoth` (docx→HTML, extra conversion step, weaker on tables),
`markitdown` (lighter, weaker layout), `unstructured` (heavy, PDF parsing historically flaky).
Docling was kept for DOCX→MD fidelity and active maintenance.

**Native text first.** A born-digital PDF already carries an exact text layer, so
`PDF_TEXT_SOURCE=auto` (default) uses it per page when the page has at least `PDF_NATIVE_MIN_CHARS`
(40) characters and sends only pages without one (scans) to the configured OCR/vision backend. The
backend is built lazily, so an all-text PDF needs no API key and no LLM/OCR call. `native` never
calls a backend (scanned pages come back empty); `image` always renders and OCRs/vision-reads every
page (the pre-ticket-30 behaviour). On the real Selling Guide chapter (44 pages) native extraction
parses in seconds and avoids Tesseract noise (bullets read as `e`/`¢`, reflowed tables).

## Out of scope (explicit)

Structured field extraction, JSON-schema output, confidence wrapping, and multi-backend
reconciliation. Do not reintroduce them in the parse path.

## Default provider: OpenRouter (free vision model)

No OpenAI/Anthropic keys. The parser's vision text-extraction defaults to **OpenRouter** with a
free image-input model. Verified 2026-09-08 with a synthetic document page against the project's
OpenRouter key:

| Model | Result |
|---|---|
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | **default — works**, verbatim extraction correct |
| `dots-studio/dots-3-note-preview:free` | worked once, then empty replies (flaky) |
| `openrouter/free` (vision router) | 200 but returned "User Safety: safe" only |
| `google/gemma-4-31b-it:free`, `gemma-4-26b-a4b-it:free` | 429 — upstream shared-pool rate-limited |
| `thinkingmachines/inkling-small:free` | 403 — agentic-harness only, not applicable |
| `nvidia/nemotron-nano-12b-v2-vl:free` | 404 — not on the live model index (listed in marketing, absent from API) |

Free-tier upstreams are flaky/rate-limited by nature. The tenacity retry (transient errors) and
configurable `PARSER_LLM_MODEL` cover this; a multi-model fallback list is a **future option**.

## Configurability (everything is config-driven)

No parser behavior is hardcoded — every knob is a runtime config value. API keys stay in
secrets; everything else is a non-secret tunable (env var in dev, `config/config.<env>.json`
in prod — same three-layer rule as the rest of the app).

| Knob | Key (env / JSON) | Default | What it controls |
|---|---|---|---|
| Vision/text-extraction provider | `PARSER_PROVIDER` | `openrouter` | Which backend extracts text from PDF page images: `openrouter` \| `openai` \| `anthropic` \| `ocr` |
| Vision model | `PARSER_LLM_MODEL` | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | Model used by the selected vision provider |
| Provider key | `OPENROUTER_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | Secret per selected provider (Key fabricated at runtime for OpenRouter) |
| Provider base URL | `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Where the OpenAI-compatible client points for OpenRouter |
| Extraction system prompt | `PARSER_SYSTEM_PROMPT` | verbatim-text prompt | Injected as the transcription/system prompt — **change it via config, never by editing backend code** |
| Backend timeout | `PARSER_TIMEOUT_SECONDS` | `60.0` | Per-call timeout for OCR/vision backends (feeds tenacity retry) |
| PDF render DPI | `PDF_DPI` | `300` | pypdfium2 page render resolution feeding OCR/vision |
| OCR language | `OCR_LANG` | `eng` | Tesseract `lang` for the `ocr` provider |
| Tesseract binary | `TESSERACT_CMD` | `""` | Override tesseract path (else system default) |
| PDF text source | `PDF_TEXT_SOURCE` | `auto` | `auto` (text layer per page, OCR/vision for scanned pages) \| `native` \| `image` |
| Native min chars | `PDF_NATIVE_MIN_CHARS` | `40` | pages with a shorter text layer are treated as scanned |
| DOCX reader | `DOCX_READER` | `docling` | DOCX → markdown reader (`docling`; alternatives config-selectable later) |

Future knob (see *Future option*): the PDF reader strategy (`image` vs `native` text
extraction), gated on cost data.

## Consequences

- The worker's `parse` step composes: **PDF** → image pages → OCR or vision LLM text; **DOCX**
  → docling Word backend → markdown; both → a common `ParseResult` (see `app/services/parser.py` seam).
- New deps: `docling-slim[format-docx,format-pdf-pypdfium2]`, `pytesseract`, `Pillow`(+), `python-docx` drops out. (`pymupdf` was kept here originally and replaced by `pypdfium2` later, see the addendum.)

## Future option (not decided)

Direct native-text PDF extraction (docling or the PDF library's text layer) for born-digital PDFs as the
*default* PDF path, with the image/OCR/vision path reserved for scanned files. Open as a config
flag discussion when cost data exists.

## Addendum (2026-11): pypdfium2 replaces PyMuPDF

PyMuPDF (and the MuPDF library it wraps) is licensed AGPL-3.0 or under a paid Artifex commercial licence. This
repository and its container images are MIT, so bundling it would have made every distributed image AGPL-encumbered
and put source-offer obligations on anyone who runs a modified copy as a network service. The PDF layer now uses
**pypdfium2** (the Python binding to Google's PDFium; Apache-2.0 / BSD-3-Clause), which docling already pulled in for its
PDF format, so no new native stack is added and the image is about 100 MB smaller.

- **Scope.** Only `services/pdf_pages.py` changed; `parser.py` and the backends see the same three functions
  (`extract_native_texts`, `page_count`, `render_page`). Native text is read per page with `get_text_range()`; pdfium's CRLF
  line ends are folded to `\n`. Pages render with `scale = dpi / 72` and are converted to a PIL RGB image.
- **Memory.** One page at a time, as before. The document, page, text page and bitmap are closed explicitly as soon as they
  are read (a test asserts every handle is closed). PDFium has no process-global image store, so the old
  `store_shrink` workaround is gone. A synthetic 200-page scan peaks at about 250 MiB (was about 256 MiB for the same file
  with PyMuPDF); a 200-page text PDF at about 132 MiB (was 165 MiB).
- **Thread safety.** PDFium is not safe for concurrent use, so a single lock still serialises all calls.
- **Text differences.** pdfium expands PDF ligatures itself (no "fi"/"fl" code points are produced; the text-normalisation
  normalisation still runs for other sources) and joins some table-cell lines with a space where MuPDF kept a newline.
  Measured on four real Selling Guide chapters: page text similarity 0.9967 to 0.9991 (mean), identical retrieval
  results, identical chunk reuse on a version update. Details are kept in the project's local smoke reports.
- **Errors.** Corrupt, empty and password-protected PDFs still surface as `DocumentLoadError("Failed to load PDF ...")`
  (the message now carries pdfium's reason, for example "Incorrect password error").
- **Guard.** `tests/unit/test_pdf_licence_guard.py` fails if `pymupdf`/`fitz` is importable, declared in `pyproject.toml`
  or resolved in `uv.lock`.
