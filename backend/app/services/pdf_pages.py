"""PDF access for the parser seam: page count, native text layer, and page renders for the vision/OCR backends.

Built on pypdfium2 (Apache-2.0 / BSD-3-Clause; ADR-0002 addendum). PDFium is not safe for concurrent use from several
threads, so every call takes one process-wide lock. Every pdfium handle (document, page, text page, bitmap) is
closed explicitly as soon as it has been read; nothing is left to the garbage collector, so a 200-page scan stays
at one page of memory (ticket 48/49).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

_RENDER_LOCK = threading.Lock()

#: PDF user space is 72 points per inch.
_POINTS_PER_INCH = 72


def extract_native_texts(path: Path) -> list[str]:
    """The text layer of every page (empty string for pages without one), in page order."""
    texts: list[str] = []
    with _RENDER_LOCK:
        doc = pdfium.PdfDocument(str(path))
        try:
            for index in range(len(doc)):
                page = doc[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        # pdfium separates lines with CRLF; the rest of the pipeline expects \n.
                        texts.append(textpage.get_text_range().replace("\r\n", "\n").replace("\r", "\n"))
                    finally:
                        textpage.close()
                finally:
                    page.close()
        finally:
            doc.close()
    return texts


def page_count(path: Path) -> int:
    """Number of pages (also validates that the file opens as a PDF). Renders nothing."""
    with _RENDER_LOCK:
        doc = pdfium.PdfDocument(str(path))
        try:
            return len(doc)
        finally:
            doc.close()


def render_page(path: Path, index: int, dpi: int) -> Image.Image:
    """Render ONE zero-based page. The parser calls this per page and drops the image after the backend has
    read it, so memory stays at one page (~25 MB at 300 dpi) instead of all pages at once (a 200-page scan
    held ~5 GB and was OOM-killed; ticket 48)."""
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    with _RENDER_LOCK:
        doc = pdfium.PdfDocument(str(path))
        try:
            page = doc[index]
            try:
                bitmap = page.render(scale=dpi / _POINTS_PER_INCH)
                try:
                    # to_pil() copies the pixels, so the PIL image stays valid after the bitmap is closed.
                    image: Image.Image = bitmap.to_pil().convert("RGB")
                    return image
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            doc.close()
