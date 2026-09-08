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
| PDF page renderer | `services/pdf_pages.py` | PDF → one RGB image per page (PyMuPDF at configurable DPI, render lock for thread-safety) |
| Backend interface | `services/backends/base.py` | `BaseBackend`: `name`, `extract_text(image) -> str` |
| OCR backend | `services/backends/ocr_backend.py` | Tesseract on a page image (scanned PDFs); needs `tesseract-ocr` + `pytesseract` |
| OpenAI-compatible vision backend | `services/backends/openai_backend.py` | Chat-completions with an image part (OpenAI SDK) |
| OpenRouter backend | `services/backends/openrouter_backend.py` | OpenAI backend pointed at `OPENROUTER_BASE_URL` — our default provider |
| Anthropic vision backend | `services/backends/anthropic_backend.py` | Messages API with an image block |
| Error taxonomy | `services/parse_errors.py` | `DocumentLoadError`, `UnsupportedFileTypeError`, `InvalidBackendError`, `BackendCallError` (transient, retried), `BackendRequestError` (permanent) |
| Retry | `services/retry.py` | tenacity: 3 attempts, exponential backoff, transient errors only, applied per page |
| Tracing | `services/observability.py` | Langfuse generation spans around LLM calls (content capture opt-in) |
| Backend factory | `services/backends/__init__.py` | `get_parser_backend(provider, settings)`; missing keys raise, never silently fall back |

**DOCX → markdown via docling** (layout-aware, tables rendered as markdown), not raw text.

Explored alternatives for DOCX: `mammoth` (docx→HTML, extra conversion step, weaker on tables),
`markitdown` (lighter, weaker layout), `unstructured` (heavy, PDF parsing historically flaky).
Docling was kept for DOCX→MD fidelity and active maintenance.

Note: docling also does strong native-text PDF extraction. We keep image → OCR/vision for PDFs,
but native extraction (docling or PyMuPDF `get_text`) is a candidate config-selectable option
for born-digital PDFs — see **Future option**.

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
| PDF render DPI | `PDF_DPI` | `300` | PyMuPDF page render resolution feeding OCR/vision |
| OCR language | `OCR_LANG` | `eng` | Tesseract `lang` for the `ocr` provider |
| Tesseract binary | `TESSERACT_CMD` | `""` | Override tesseract path (else system default) |
| DOCX reader | `DOCX_READER` | `docling` | DOCX → markdown reader (`docling`; alternatives config-selectable later) |

Future knob (see *Future option*): the PDF reader strategy (`image` vs `native` text
extraction), gated on cost data.

## Consequences

- The worker's `parse` step composes: **PDF** → image pages → OCR or vision LLM text; **DOCX**
  → docling → markdown; both → a common `ParseResult` (see `app/services/parser.py` seam).
- New deps: `docling`, `pytesseract`, `Pillow`(+), keep `pymupdf`; `python-docx` drops out.

## Future option (not decided)

Direct native-text PDF extraction (docling or PyMuPDF `get_text`) for born-digital PDFs as the
*default* PDF path, with the image/OCR/vision path reserved for scanned files. Open as a config
flag discussion when cost data exists.