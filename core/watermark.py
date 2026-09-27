"""Stamps a semi-transparent, diagonal text watermark onto every page of a
PDF ("Водяные знаки" — README "Каталог услуг" item 9 / Фаза 4): the
distributor-facing use case is a downloaded price list or накладная
carrying the downloader's ID/email/IP so a leaked copy can be traced.

PDF-page library choice — benchmarked for real, not picked from
descriptions (same discipline as camelot vs. pdfplumber earlier in this
project, see README "PDF — реально протестировано"). Candidates: `pypdf`
(actively maintained PyPDF2 fork) and `pikepdf` (libqpdf wrapper).
Measured on this phase's two actual tasks, 200-page synthetic PDF,
generated with reportlab + a registered Cyrillic TTF:

  watermark, 200 pages   pypdf:   0.222s   pikepdf: 0.081s  (pikepdf ~2.7x faster)
  split-by-marker (needs per-page text extraction): pikepdf has NO text
    extraction API at all (`hasattr` check on Page/Pdf objects: False) —
    it's a structural/QDF editing library, not a text-layout engine.

Both libraries' watermark output verified correct by reading the result
back and confirming the watermark string is actually present in the
extracted text of every page (200/200 for both).

Decision: pypdf for BOTH tools (this module and core/pdf_split.py). Split
categorically needs text extraction, which only pypdf has; watermarking
alone would favor pikepdf's raw speed, but 140ms over 200 pages is not a
real difference at this project's scale (single-digit-page uploads on the
free tier, per README's own throughput tables), and shipping one
well-understood dependency instead of two for two closely related PDF
tools is the simpler, more maintainable choice — not a default, a measured
tradeoff.
"""

from __future__ import annotations

import math
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

# Same font resolution as core/mailmerge.py / generate_fixtures.py.
_CYRILLIC_FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
]

_FONT_NAME = "GruperWatermarkFont"
_font_registered = False


class WatermarkError(ValueError):
    pass


def _ensure_font() -> str:
    global _font_registered
    if _font_registered:
        return _FONT_NAME
    for candidate in _CYRILLIC_FONT_CANDIDATES:
        if candidate.exists():
            pdfmetrics.registerFont(TTFont(_FONT_NAME, str(candidate)))
            _font_registered = True
            return _FONT_NAME
    raise WatermarkError(
        "No Cyrillic-capable TTF font found on this machine — refusing to "
        "render a watermark, since reportlab's default Helvetica would "
        "silently mangle Russian text instead of raising."
    )


def _fit_font_size(text: str, font: str, width: float, height: float) -> float:
    """Picks a font size so the watermark's diagonal run spans a fixed
    fraction of the page's own diagonal, regardless of page size or text
    length — a one-word watermark ("ЧЕРНОВИК") and a long one (an email
    address) both end up readable but not overflowing the page."""
    diagonal = math.hypot(width, height)
    target_width = diagonal * 0.8
    probe_size = 60.0
    probe_width = pdfmetrics.stringWidth(text, font, probe_size)
    if probe_width <= 0:
        return probe_size
    size = probe_size * (target_width / probe_width)
    return max(14.0, min(size, 140.0))


def _make_overlay_page(width: float, height: float, text: str):
    font = _ensure_font()
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(width, height))
    c.saveState()
    font_size = _fit_font_size(text, font, width, height)
    c.setFont(font, font_size)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    try:
        c.setFillAlpha(0.35)
    except AttributeError:  # pragma: no cover - very old reportlab only
        pass
    c.translate(width / 2, height / 2)
    c.rotate(45)
    c.drawCentredString(0, 0, text)
    c.restoreState()
    c.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def add_watermark(pdf_bytes: bytes, text: str) -> bytes:
    """Returns a new PDF (same page count and order as the input) with
    `text` overlaid diagonally, semi-transparent, on every page. One
    overlay is rendered per distinct page size and reused across all pages
    of that size (cheap re-use, not a hand-rolled cache micro-optimization
    that matters at these file sizes — it just avoids re-running reportlab
    once per page when nearly all real PDFs are one uniform page size)."""
    text = text.strip()
    if not text:
        raise WatermarkError("Текст водяного знака не может быть пустым")

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
    except (PdfReadError, ValueError) as exc:
        raise WatermarkError(f"Не удалось прочитать PDF: {exc}") from exc

    if reader.is_encrypted:
        raise WatermarkError("PDF защищён паролем — наложение водяного знака невозможно")

    if len(reader.pages) == 0:
        raise WatermarkError("PDF не содержит страниц")

    # clone_from (not building an empty PdfWriter + add_page in a loop)
    # attaches every page to the writer up front — merge_page() on a page
    # that isn't yet owned by a writer is deprecated as of pypdf 6.x and
    # slated for removal in 7.0 (verified live: it still works today but
    # warns). Merging the overlay onto writer.pages[i] instead is the
    # currently-recommended, non-deprecated shape of the same operation.
    writer = PdfWriter(clone_from=reader)
    overlay_cache: dict[tuple[float, float], object] = {}
    for page in writer.pages:
        size = (float(page.mediabox.width), float(page.mediabox.height))
        overlay = overlay_cache.get(size)
        if overlay is None:
            overlay = _make_overlay_page(size[0], size[1], text)
            overlay_cache[size] = overlay
        page.merge_page(overlay)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()
