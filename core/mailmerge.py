"""Mail Merge ("Именные документы" in the Инструменты catalog — see README
"Каталог услуг" item 10 / Фаза 4): an Excel file with one row per recipient
and a simple `{{Поле}}` text template turns into N personalized PDFs.

Deliberately NOT a PDF/Word form-filler. Filling an arbitrary uploaded
template document is a much harder, more fragile problem (discovering form
fields, matching fonts/layout the form wasn't built to accept, handling
templates that have no fillable fields at all). A plain text template
compiled fresh into a brand-new PDF via reportlab — already a project
dependency, previously test-fixture-only, now promoted to a runtime one
(see requirements.txt) — is the right scope for this phase: it covers the
real "договор/справка/уведомление на N человек" use case without pretending
to be a general document-editing engine. Matches the project's existing
"measure the actual task, don't over-build" discipline (see the PDF-page
library choice in core/pdf_split.py and core/watermark.py).

Cyrillic font handling reuses the exact pattern already established in
backend/tests/fixtures/generate_fixtures.py: reportlab's built-in
Helvetica has no Cyrillic glyphs at all and silently mangles Russian text
instead of raising, a trap this project already documented once — so a
TTF with Cyrillic coverage is registered explicitly before any drawing.
"""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

# Same candidate list/order as generate_fixtures.py's
# _register_cyrillic_font(): Windows dev machine first, then the common
# Debian/Ubuntu server locations (the deploy target has fonts-dejavu-core
# installed — verified live via `fc-list`/`ls /usr/share/fonts/truetype/`
# on 195.161.62.21).
_CYRILLIC_FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
]

_FONT_NAME = "GruperMailMergeFont"
_font_registered = False


class MailMergeError(ValueError):
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
    raise MailMergeError(
        "No Cyrillic-capable TTF font found on this machine — refusing to "
        "render PDFs, since reportlab's default Helvetica would silently "
        "mangle Russian text instead of raising."
    )


def extract_placeholders(template: str) -> list[str]:
    """Column names referenced by `{{Поле}}` in `template`, in first-seen
    order, de-duplicated. Whitespace inside the braces is tolerated
    (`{{ Сумма }}` == `{{Сумма}}`) but not inside the name itself."""
    seen: list[str] = []
    for match in PLACEHOLDER_RE.finditer(template):
        name = match.group(1)
        if name and name not in seen:
            seen.append(name)
    return seen


def validate_template(template: str, available_columns: list[str]) -> list[str]:
    """Raises MailMergeError if the template has no placeholders at all, or
    references a column name the uploaded file doesn't actually have (a
    typo, or the user editing the template after re-picking the header
    row). Returns the placeholder list on success so the caller doesn't
    need to re-parse."""
    placeholders = extract_placeholders(template)
    if not placeholders:
        raise MailMergeError("В шаблоне нет ни одного плейсхолдера вида {{Поле}}")

    unknown = [p for p in placeholders if p not in available_columns]
    if unknown:
        raise MailMergeError(
            "Шаблон ссылается на колонки, которых нет в файле: " + ", ".join(unknown)
        )
    return placeholders


def render_text(template: str, row: dict[str, str]) -> str:
    """Substitutes every `{{Поле}}` with `row["Поле"]`. A placeholder whose
    column is blank for this particular row renders as an empty string,
    not a literal "None" — validate_template() already guarantees every
    placeholder maps to a real column, so a missing key here only happens
    for a genuinely blank cell."""

    def _replace(match: re.Match) -> str:
        return row.get(match.group(1), "")

    return PLACEHOLDER_RE.sub(_replace, template)


def build_row_dicts(column_names: list[str], rows: list[list]) -> list[dict[str, str]]:
    """Turns each data row (already header-promoted — see
    backend/columns.py::promote_header_row) into a {column_name: value}
    dict ready for render_text(). Every cell is stringified once here (raw
    parse_file_raw() cells already arrive as strings for Excel/CSV — see
    core/parsing.py — this just also covers None -> "" uniformly rather
    than assuming that dtype)."""
    result: list[dict[str, str]] = []
    for row in rows:
        result.append({name: ("" if value is None else str(value)) for name, value in zip(column_names, row)})
    return result


def _wrap_line(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Greedy word-wrap measured with the real font metrics (not a fixed
    character count) so long template lines don't run off the page edge.
    Kept intentionally simple — no hyphenation/justification — this is a
    Mail Merge letter body, not a typesetting engine."""
    if not text:
        return [""]
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or pdfmetrics.stringWidth(candidate, font, size) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def render_pdf(text: str, font_size: int = 12) -> bytes:
    """Compiles a plain multi-line, word-wrapped, paginated PDF from
    already-substituted text. One document per recipient — the caller
    (backend/main.py) calls this once per row and zips the results."""
    font = _ensure_font()
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin_x = 64
    top_y = height - 72
    bottom_y = 64
    line_height = font_size * 1.5
    max_width = width - 2 * margin_x

    c.setFont(font, font_size)
    y = top_y
    for paragraph in text.split("\n"):
        for line in _wrap_line(paragraph, font, font_size, max_width):
            if y < bottom_y:
                c.showPage()
                c.setFont(font, font_size)
                y = top_y
            c.drawString(margin_x, y, line)
            y -= line_height
    c.save()
    return buffer.getvalue()
