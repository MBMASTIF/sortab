"""core.watermark tests. See tests/test_pdf_split.py's module docstring
for why these build their own tiny PDFs via reportlab directly rather than
loading a committed fixture — reportlab is a runtime dependency here now,
not a fixture-authoring-only tool."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from core.watermark import WatermarkError, add_watermark

_CYRILLIC_FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
]

_FONT = "TestCyrillicFont2"
_registered = False


def _font() -> str:
    global _registered
    if _registered:
        return _FONT
    for candidate in _CYRILLIC_FONT_CANDIDATES:
        if candidate.exists():
            pdfmetrics.registerFont(TTFont(_FONT, str(candidate)))
            _registered = True
            return _FONT
    pytest.skip("No Cyrillic TTF font available on this machine")


def _make_pdf(n_pages: int, pagesize=A4) -> bytes:
    font = _font()
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=pagesize)
    for i in range(n_pages):
        c.setFont(font, 12)
        c.drawString(70, 780 if pagesize == A4 else 700, f"Страница {i + 1}: сумма 1234.56 руб.")
        c.showPage()
    c.save()
    return buffer.getvalue()


def test_add_watermark_preserves_page_count():
    src = _make_pdf(3)
    result = add_watermark(src, "ЧЕРНОВИК")
    reader = PdfReader(BytesIO(result))
    assert len(reader.pages) == 3


def test_add_watermark_text_appears_on_every_page():
    src = _make_pdf(4)
    result = add_watermark(src, "ЧЕРНОВИК")
    reader = PdfReader(BytesIO(result))
    for page in reader.pages:
        text = page.extract_text() or ""
        assert "ЧЕРНОВИК" in text


def test_add_watermark_preserves_original_content():
    src = _make_pdf(1)
    result = add_watermark(src, "user@example.com")
    reader = PdfReader(BytesIO(result))
    text = reader.pages[0].extract_text() or ""
    assert "1234.56" in text
    assert "user@example.com" in text


def test_add_watermark_handles_mixed_page_sizes():
    """Realistic edge case: not every PDF has uniform page size. Builds one
    document with an A4 page and a LETTER page, confirms the watermark
    (and each page's own original content) survives on both."""
    font = _font()
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setFont(font, 12)
    c.drawString(70, 780, "страница A4")
    c.showPage()
    c.setPageSize(LETTER)
    c.setFont(font, 12)
    c.drawString(70, 700, "страница LETTER")
    c.showPage()
    c.save()
    src = buffer.getvalue()

    result = add_watermark(src, "КОПИЯ")
    reader = PdfReader(BytesIO(result))
    assert len(reader.pages) == 2
    text0 = reader.pages[0].extract_text() or ""
    text1 = reader.pages[1].extract_text() or ""
    assert "страница A4" in text0 and "КОПИЯ" in text0
    assert "страница LETTER" in text1 and "КОПИЯ" in text1


def test_add_watermark_rejects_empty_text():
    src = _make_pdf(1)
    with pytest.raises(WatermarkError):
        add_watermark(src, "   ")


def test_add_watermark_rejects_garbage_bytes():
    with pytest.raises(WatermarkError):
        add_watermark(b"not a pdf", "ЧЕРНОВИК")
