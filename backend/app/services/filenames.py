"""Filename handling shared by uploads and URL ingest."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ALLOWED_EXTENSIONS: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "md": "text/markdown",
}


def safe_name(name: str) -> str:
    """A bare, filesystem- and key-safe file name: no directories, no traversal, no odd characters."""
    base = Path(name.replace("\x00", "").replace("\\", "/")).name
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .")
    return cleaned[:200] or "upload"


def name_from_url_path(path: str) -> str:
    """Last path segment of a URL path, percent-decoded then sanitised ('' when there is none)."""
    segment = path.rstrip("/").rsplit("/", 1)[-1]
    return safe_name(unquote(segment)) if segment else ""
