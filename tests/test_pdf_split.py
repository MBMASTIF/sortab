"""core.pdf_split tests. Builds its own tiny multi-page PDFs via reportlab
directly (not committed static fixtures) — unlike the Excel/CSV/camelot
fixtures in backend/tests/fixtures/, reportlab is now a RUNTIME dependency
of this project (core/mailmerge.py, core/watermark.py both need it to
actually render output), not just a fixture-authoring tool, so there's no
reason to avoid it inside the test suite itself. A registered Cyrillic TTF
is still required — same documented trap as everywhere else in this repo:
Helvetica has no Cyrillic glyphs.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from core.pdf_split import SplitError, split_by_marker

_CYRILLIC_FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
]

_FONT = "TestCyrillicFont"
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


def _make_invoices_pdf(invoices: list[tuple[str, int]]) -> bytes:
    """invoices: list of (marker_text_or_None, extra_page_count) — first
    page of each invoice carries the marker (unless None), plus
    extra_page_count additional pages with no marker."""
    font = _font()
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    for marker_text, extra_pages in invoices:
        c.setFont(font, 12)
        if marker_text:
            c.drawString(70, 780, marker_text)
        c.drawString(70, 750, "содержимое страницы 1")
        c.showPage()
        for i in range(extra_pages):
            c.setFont(font, 12)
            c.drawString(70, 780, f"продолжение, страница {i + 2}")
            c.showPage()
    c.save()
    return buffer.getvalue()


def test_split_by_marker_produces_one_part_per_marker_occurrence():
    pdf_bytes = _make_invoices_pdf(
        [("Накладная № 0001", 1), ("Накладная № 0002", 0), ("Накладная № 0003", 2)]
    )
    parts = split_by_marker(pdf_bytes, "Накладная №")
    assert len(parts) == 3
    assert [p.page_count for p in parts] == [2, 1, 3]
    assert sum(p.page_count for p in parts) == 6


def test_split_by_marker_each_part_contains_correct_content():
    pdf_bytes = _make_invoices_pdf([("Накладная № 0001", 0), ("Накладная № 0002", 0)])
    parts = split_by_marker(pdf_bytes, "Накладная №")

    reader1 = PdfReader(BytesIO(parts[0].pdf_bytes))
    text1 = reader1.pages[0].extract_text() or ""
    assert "0001" in text1

    reader2 = PdfReader(BytesIO(parts[1].pdf_bytes))
    text2 = reader2.pages[0].extract_text() or ""
    assert "0002" in text2


def test_split_by_marker_keeps_leading_pages_before_first_marker():
    pdf_bytes = _make_invoices_pdf([(None, 1), ("Накладная № 0001", 0)])
    parts = split_by_marker(pdf_bytes, "Накладная №")
    # page 0 and 1 (no marker) become their own leading part, page 2 (the
    # marker) starts the next one — no page silently dropped.
    assert len(parts) == 2
    assert parts[0].page_count == 2
    assert parts[1].page_count == 1


def test_split_by_marker_raises_when_marker_not_found():
    pdf_bytes = _make_invoices_pdf([("Накладная № 0001", 0)])
    with pytest.raises(SplitError):
        split_by_marker(pdf_bytes, "Совсем другой текст, которого нет")


def test_split_by_marker_rejects_empty_marker():
    pdf_bytes = _make_invoices_pdf([("Накладная № 0001", 0)])
    with pytest.raises(SplitError):
        split_by_marker(pdf_bytes, "   ")


def test_split_by_marker_rejects_garbage_bytes():
    with pytest.raises(SplitError):
        split_by_marker(b"this is not a pdf at all", "Накладная")
