"""Backend interface: turn one page image into text."""

from __future__ import annotations

import base64
import io
from abc import ABC, abstractmethod

from PIL import Image


class BaseBackend(ABC):
    """A text-extraction backend (vision LLM or OCR)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short provider id, e.g. ``openrouter``."""

    @abstractmethod
    def extract_text(self, image: Image.Image) -> str:
        """Return the text found in ``image``."""


def image_to_png_base64(image: Image.Image) -> str:
    """PNG-encode ``image`` and return it base64-encoded (no data-URI prefix)."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")
