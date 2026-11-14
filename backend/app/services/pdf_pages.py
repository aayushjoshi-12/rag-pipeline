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


def render_pages(path: Path, indices: list[int], dpi: int) -> dict[int, Image.Image]:
    """Render only the given zero-based pages (the ones that need OCR/vision)."""
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
