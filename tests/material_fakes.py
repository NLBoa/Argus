"""Build small real PDF, PPTX, and DOCX files in memory for extraction tests."""

from __future__ import annotations

import io
import zipfile


def pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF with one line of Helvetica text per page."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font_id = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objects.append(
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def pptx(slides: list[tuple[str, str]], notes: str | None = None) -> bytes:
    from pptx import Presentation

    deck = Presentation()
    for title, body in slides:
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
    buf = io.BytesIO()
    deck.save(buf)
    return buf.getvalue()


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def docx_xml(body: str, *, prolog: str = "") -> bytes:
    """A `word/document.xml` around `body` (WordprocessingML with `w:` and `mc:` prefixes)."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        + prolog
        + f'<w:document xmlns:w="{W_NS}" xmlns:mc="{MC_NS}"><w:body>{body}</w:body></w:document>'
    ).encode()


def para(*runs: str) -> str:
    """A paragraph with one run per text."""
    return "<w:p>" + "".join(f"<w:r><w:t>{r}</w:t></w:r>" for r in runs) + "</w:p>"


def docx(document: bytes | None, *, pages: int | None = None, extra: int = 0) -> bytes:
    """A minimal .docx zip: the document part, Word's page count if given, and `extra` bytes
    of filler in a part of their own."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        if document is not None:
            z.writestr("word/document.xml", document)
        if pages is not None:
            z.writestr("docProps/app.xml", f"<Properties><Pages>{pages}</Pages></Properties>")
        if extra:
            z.writestr("word/media/filler.bin", b"\0" * extra)
    return buf.getvalue()
