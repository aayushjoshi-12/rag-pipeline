"""Licence guard: PyMuPDF / MuPDF is AGPL-3.0 and must never come back into the project or the images (ticket 60)."""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
_FORBIDDEN = ("pymupdf", "fitz", "pymupdf4llm", "mupdf")


@pytest.mark.parametrize("name", _FORBIDDEN)
def test_agpl_pdf_library_is_not_importable(name: str) -> None:
    assert importlib.util.find_spec(name) is None, (
        f"{name} (AGPL) is installed: remove it from the environment"
    )


def test_pyproject_does_not_declare_an_agpl_pdf_library() -> None:
    data = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    declared = list(data["project"]["dependencies"])
    for group in data.get("dependency-groups", {}).values():
        declared += [d for d in group if isinstance(d, str)]
    for extra in data["project"].get("optional-dependencies", {}).values():
        declared += extra
    names = {
        re.split(r"[\s\[<>=!~;]", d, maxsplit=1)[0].lower().replace("_", "-") for d in declared
    }
    assert not names & set(_FORBIDDEN)


def test_lockfile_does_not_resolve_an_agpl_pdf_library() -> None:
    lock = tomllib.loads((BACKEND / "uv.lock").read_text())
    packages = {p["name"].lower() for p in lock["package"]}
    assert not packages & set(_FORBIDDEN)
    assert "pypdfium2" in packages  # the permissive replacement is really there


def test_pypdfium2_is_a_direct_dependency() -> None:
    data = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    assert any(d.lower().startswith("pypdfium2") for d in data["project"]["dependencies"])
