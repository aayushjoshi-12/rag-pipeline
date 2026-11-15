"""Minimal PDF writer for tests (no PDF library needed): text pages, image-only (scanned) pages, blank pages.

Text uses the built-in Helvetica font, so a page has a real text layer; an image page carries only a raster.
"""

from __future__ import annotations

import zlib
from pathlib import Path

from PIL import Image

#: A4 in PDF points (72 per inch).
PAGE_W, PAGE_H = 595, 842

PageSpec = str | Image.Image | None


def _escape(line: str) -> bytes:
    data = line.encode("cp1252", errors="replace")
    return data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _text_stream(text: str, size: int = 11) -> bytes:
    lines = text.split("\n")
    out = [
        b"BT",
        f"/F1 {size} Tf".encode(),
        f"72 {PAGE_H - 72} Td".encode(),
        f"{size + 3} TL".encode(),
    ]
    for line in lines:
        out.append(b"(" + _escape(line) + b") Tj T*")
    out.append(b"ET")
    return b"\n".join(out)


def build_pdf(pages: list[PageSpec], *, encrypted: bool = False) -> bytes:
    """Bytes of a PDF with one page per entry: ``str`` = text page, ``Image`` = scanned page, ``None`` = blank.

    ``encrypted`` adds a Standard-security-handler dictionary whose password nobody knows (opening needs one)."""
    objects: list[bytes] = []  # object number = index + 1

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")  # 1
    add(b"")  # 2: Pages, filled below
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")  # 3

    kids: list[int] = []
    for spec in pages:
        resources = b"/Font << /F1 3 0 R >>"
        if isinstance(spec, Image.Image):
            rgb = spec.convert("RGB")
            raw = zlib.compress(rgb.tobytes())
            img = add(
                f"<< /Type /XObject /Subtype /Image /Width {rgb.width} /Height {rgb.height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(raw)} >>\n".encode()
                + b"stream\n"
                + raw
                + b"\nendstream"
            )
            resources += f" /XObject << /Im0 {img} 0 R >>".encode()
            content = f"q {PAGE_W - 144} 0 0 {int((PAGE_W - 144) * rgb.height / rgb.width)} 72 72 cm /Im0 Do Q".encode()
        elif spec is None:
            content = b""
        else:
            content = _text_stream(spec)
        stream = add(
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
        page = add(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] ".encode()
            + b"/Resources << "
            + resources
            + b" >> "
            + f"/Contents {stream} 0 R >>".encode()
        )
        kids.append(page)

    encrypt_ref = b""
    if encrypted:
        enc = add(
            b"<< /Filter /Standard /V 1 /R 2 /P -4 /O <"
            + b"41" * 32
            + b"> /U <"
            + b"42" * 32
            + b"> >>"
        )
        encrypt_ref = f" /Encrypt {enc} 0 R /ID [<{'00' * 16}> <{'00' * 16}>]".encode()

    kid_refs = " ".join(f"{k} 0 R" for k in kids)
    objects[1] = f"<< /Type /Pages /Kids [{kid_refs}] /Count {len(kids)} >>".encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R".encode() + encrypt_ref
    out += f" >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def write_pdf(path: Path, pages: list[PageSpec], *, encrypted: bool = False) -> Path:
    path.write_bytes(build_pdf(pages, encrypted=encrypted))
    return path
