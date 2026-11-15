"""Parse seam — turns a single file into text/markdown for chunking.

Adapts the PDF-page renderer, the vision/OCR backends and docling into a common
``ParseResult``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.config import Settings
from app.services.backends import BaseBackend, get_parser_backend
from app.services.langfuse import configure_from_settings
from app.services.parse_errors import DocumentLoadError, UnsupportedFileTypeError
from app.services.pdf_pages import extract_native_texts, page_count, render_page
from app.services.retry import with_retry
from app.services.textnorm import normalise_text

logger = logging.getLogger(__name__)

#: Delimiter between per-page texts in ``ParseResult.text``.
_PAGE_SEPARATOR = "\n\n--- PAGE BREAK ---\n\n"

_SUPPORTED_FILETYPES = ("pdf", "docx", "txt", "md")

ParseFormat = Literal["text", "markdown"]


@dataclass
class ParseResult:
    text: str
    format: ParseFormat
    filetype: str
    provider: str
    page_count: int | None = None
    page_texts: list[str] = field(default_factory=list)


def parse(
    path: Path,
    filetype: str,
    settings: Settings | None = None,
    provider: str | None = None,
    backend: BaseBackend | None = None,
) -> ParseResult:
    """Parse ``path`` (given by ``filetype``) into a common ``ParseResult``.

    PDF → page images → the configured vision/OCR backend. DOCX → docling → markdown.
    ``backend`` is a test/injection hook that skips provider selection when given.
    """
    cfg = settings or Settings()
    configure_from_settings(cfg)
    normalized = filetype.lower().lstrip(".")

    if normalized == "pdf":
        result = _parse_pdf(path, cfg, provider, backend)
    elif normalized == "docx":
        result = _parse_docx(path, cfg)
    elif normalized in ("txt", "md"):
        result = _parse_text(path, normalized)
    else:
        raise UnsupportedFileTypeError(
            f"Unsupported file type '{filetype}'. Supported: {', '.join(_SUPPORTED_FILETYPES)}."
        )
    return _normalised(result) if cfg.parse_normalize_text else result


def _normalised(result: ParseResult) -> ParseResult:
    """Fold ligatures / odd spaces / curly quotes (``services/textnorm.py``) in every text of ``result``.

    Done once, here at the seam, so the chunker, the embedder and the keyword index all see the same
    characters a user would type. Folding per page and joining gives the same text as folding the whole
    (the page separator is ASCII)."""
    result.page_texts = [normalise_text(t) for t in result.page_texts]
    result.text = normalise_text(result.text)
    return result


def _parse_pdf(
    path: Path,
    cfg: Settings,
    provider: str | None,
    backend: BaseBackend | None,
) -> ParseResult:
    """PDF → per-page text.

    ``auto`` (default) takes a page's own text layer when it has enough text and only sends pages
    without one (scans) to the OCR/vision backend, which is built lazily — a born-digital PDF needs
    no API key and no OCR. ``native`` never calls a backend; ``image`` always does.
    """
    mode = cfg.pdf_text_source
    try:
        native = extract_native_texts(path) if mode != "image" else None
        if native is None:
            page_total = page_count(path)  # validates + counts, renders nothing
            texts: list[str | None] = [None] * page_total
        else:
            page_total = len(native)
            texts = [
                t.strip() if (mode == "native" or len(t.strip()) >= cfg.pdf_native_min_chars) else None
                for t in native
            ]
        need = [i for i, t in enumerate(texts) if t is None]
        if cfg.pdf_dpi <= 0:
            raise ValueError("dpi must be positive")
    except Exception as exc:
        raise DocumentLoadError(f"Failed to load PDF {path}: {exc}") from exc

    _backend: BaseBackend | None = None
    for i in need:
        if _backend is None:
            _backend = backend or get_parser_backend(provider=provider, settings=cfg)
        logger.debug("Extracting text from page %d/%d via %s", i + 1, page_total, _backend.name)
        # One page image at a time, freed before the next render: a 200-page scan must not sit in RAM at once.
        try:
            image = render_page(path, i, cfg.pdf_dpi)
        except Exception as exc:
            raise DocumentLoadError(f"Failed to render page {i + 1} of PDF {path}: {exc}") from exc
        try:
            # Retry transient backend failures (timeouts + translated call errors,
            # incl. free-tier models returning empty/missing completions) per page.
            texts[i] = with_retry(_backend.extract_text, image)
        except Exception as exc:
            raise DocumentLoadError(
                f"{_backend.name} backend failed on page {i + 1} of {path}: {exc}"
            ) from exc
        finally:
            del image
        logger.debug("Page %d: %d chars", i + 1, len(texts[i] or ""))

    page_texts = [t or "" for t in texts]
    text = _PAGE_SEPARATOR.join(page_texts)
    native_pages = page_total - len(need)
    if _backend is None:
        used = "native"
    elif native_pages:
        used = f"native+{_backend.name}"
    else:
        used = _backend.name
    logger.info(
        "Parsed PDF %s via %s: %d page(s) (%d native, %d via backend), %d chars",
        path.name, used, page_total, native_pages, len(need), len(text),
    )
    return ParseResult(
        text=text,
        format="text",
        filetype="pdf",
        provider=used,
        page_count=page_total,
        page_texts=page_texts,
    )


def _parse_docx(path: Path, cfg: Settings) -> ParseResult:
    if cfg.docx_reader != "docling":
        raise DocumentLoadError(f"Unknown DOCX reader '{cfg.docx_reader}'. Supported: docling.")
    try:
        # Use docling's Word backend directly (an XML → document converter) rather than
        # DocumentConverter, which also imports the OCR/layout-model pipelines (torch, scipy…)
        # that DOCX conversion never uses.
        from docling.backend.msword_backend import MsWordDocumentBackend
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.document import InputDocument

        in_doc = InputDocument(
            path_or_stream=path, format=InputFormat.DOCX, backend=MsWordDocumentBackend
        )
        backend = MsWordDocumentBackend(in_doc=in_doc, path_or_stream=path)
        markdown = backend.convert().export_to_markdown()
    except Exception as exc:
        raise DocumentLoadError(f"Failed to convert DOCX {path} with docling: {exc}") from exc

    logger.info("Parsed DOCX %s via docling: %d chars", path.name, len(markdown))
    return ParseResult(
        text=markdown,
        format="markdown",
        filetype="docx",
        provider="docling",
        page_count=None,
        page_texts=[],
    )


def _parse_text(path: Path, filetype: str) -> ParseResult:
    """Plain text / markdown: read as UTF-8 (undecodable bytes replaced), no backend call."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DocumentLoadError(f"Failed to read {path}: {exc}") from exc
    return ParseResult(
        text=text,
        format="markdown" if filetype == "md" else "text",
        filetype=filetype,
        provider="plain",
    )


__all__ = [
    "ParseFormat",
    "ParseResult",
    "parse",
]
