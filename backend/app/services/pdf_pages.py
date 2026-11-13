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
