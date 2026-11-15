"""Render PDF pages to PIL images for the vision/OCR backends."""

from __future__ import annotations

import threading
from pathlib import Path

import pymupdf
from PIL import Image

# MuPDF's global state is not safe for concurrent rendering across threads.
_RENDER_LOCK = threading.Lock()


class PdfPageRenderer:
    """Turns a PDF file into one RGB image per page at a fixed DPI."""

    def __init__(self, dpi: int = 200) -> None:
        if dpi <= 0:
            raise ValueError("dpi must be positive")
        self.dpi = dpi

    def load(self, path: Path) -> list[Image.Image]:
        with _RENDER_LOCK, pymupdf.open(str(path)) as doc:  # type: ignore[no-untyped-call]  # pymupdf ships no type hints
            images: list[Image.Image] = []
            for page in doc:
                pix = page.get_pixmap(dpi=self.dpi)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                images.append(img)
            return images


def extract_native_texts(path: Path) -> list[str]:
    """The text layer of every page (empty string for pages without one), in page order."""
    with _RENDER_LOCK, pymupdf.open(str(path)) as doc:  # type: ignore[no-untyped-call]  # pymupdf ships no type hints
        return [str(page.get_text("text")) for page in doc]


def page_count(path: Path) -> int:
    """Number of pages (also validates that the file opens as a PDF). Renders nothing."""
    with _RENDER_LOCK, pymupdf.open(str(path)) as doc:  # type: ignore[no-untyped-call]  # pymupdf ships no type hints
        return int(doc.page_count)


def render_page(path: Path, index: int, dpi: int) -> Image.Image:
    """Render ONE zero-based page. The parser calls this per page and drops the image after the backend has
    read it, so memory stays at one page (~25 MB at 300 dpi) instead of all pages at once (a 200-page scan
    held ~5 GB and was OOM-killed; ticket 48)."""
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    with _RENDER_LOCK, pymupdf.open(str(path)) as doc:  # type: ignore[no-untyped-call]  # pymupdf ships no type hints
        pix = doc.load_page(index).get_pixmap(dpi=dpi)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        # MuPDF keeps every decoded page image in a process-global store (up to 256 MB, then more across
        # documents); without this a 60-page scan held ~600 MB RSS after rendering, flat at ~115 MB with it.
        pymupdf.TOOLS.store_shrink(100)  # type: ignore[no-untyped-call]  # pymupdf ships no type hints
        return image


def render_pages(path: Path, indices: list[int], dpi: int) -> dict[int, Image.Image]:
    """Render the given zero-based pages at once. Holds every image in memory: only for small page sets
    (tests, previews); the parser uses ``render_page`` one page at a time."""
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    wanted = set(indices)
    out: dict[int, Image.Image] = {}
    with _RENDER_LOCK, pymupdf.open(str(path)) as doc:  # type: ignore[no-untyped-call]  # pymupdf ships no type hints
        for i, page in enumerate(doc):
            if i in wanted:
                pix = page.get_pixmap(dpi=dpi)
                out[i] = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return out
