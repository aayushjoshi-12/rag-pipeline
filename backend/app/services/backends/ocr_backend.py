"""Tesseract OCR backend — local, no network, no API key."""

from __future__ import annotations

import pytesseract
from PIL import Image

from app.services.backends.base import BaseBackend
from app.services.parse_errors import BackendRequestError


class OCRBackend(BaseBackend):
    def __init__(self, tesseract_cmd: str | None = None, lang: str = "eng") -> None:
        self.lang = lang
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    @property
    def name(self) -> str:
        return "ocr"

    def extract_text(self, image: Image.Image) -> str:
        try:
            return str(pytesseract.image_to_string(image, lang=self.lang))
        except pytesseract.TesseractNotFoundError as exc:
            raise BackendRequestError(f"Tesseract executable not found: {exc}") from exc
