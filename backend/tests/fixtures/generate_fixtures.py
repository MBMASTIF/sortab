"""Generates the small library of realistic test files this project uses
for manual and Playwright end-to-end testing (backend/tests/fixtures/*).

These are NOT synthetic edge cases invented in the abstract — each one
reproduces a pattern already documented elsewhere in this codebase as a
real thing that happens with real Russian marketplace/1C exports:

- decorative_header_1c.xlsx: a report-title row sitting above the real
  header row. Exactly the shape core.parsing.parse_file_raw() and
  backend/columns.py exist to handle (see their docstrings), and the same
  shape backend/tests/test_api.py's hand-built fixture and
  design-prototype/upload.html's mock data both use.

- sales_export_cp1251.csv: a Windows-1251, semicolon-delimited CSV with
  "1 234,56"-style numbers (space thousands separator, comma decimal) —
  the real default shape of a 1C export. Exercises
  core.parsing._detect_csv_dialect()'s cp1251-before-charset-normalizer
  path (see its docstring: charset-normalizer alone mis-detected a real
  cp1251 sample as cp1255/Hebrew) and columns.py's
  normalize_decimal_comma().

- similar_names_case_whitespace.xlsx: the same real product written three
  different ways — different case, trailing whitespace — the exact
  "user manually merges variant spellings into one group" scenario
  core/session.py's entity grouping targets, and the one place in this
  project where entity *identity* (not just formatting) is on the line.
  See README/tree.py: fuzzy/normalized matching is explicitly Phase 3 and
  not implemented yet — entities here group by exact string equality, so
  this fixture also documents, by construction, that these three rows are
  three distinct entities today, not one.

- large_fractional_sums.xlsx: many rows of fractional-kopeck amounts whose
  total is known in advance and was computed with Decimal, not float — a
  regression fixture for money arithmetic drift on the full
  upload -> assign -> export path (core/reconcile.py, core/export.py both
  use Decimal end to end).

- pdf_invoice_no_borders.pdf: a single-page table (header + 1 data row)
  drawn with plain positioned text — NO ruled lines/rectangles around the
  cells, exactly like most real invoices/накладные. Exists to prove
  core.parsing._read_pdf_raw()'s flavor="stream" choice is actually load
  -bearing: README documents that camelot's default border-seeking
  behavior (and pdfplumber's default entirely) finds 0 rows on this exact
  shape — flavor="stream" (text-position based) is the one that works.

- pdf_multipage_table.pdf: one logical table split across 3 pages with the
  header row repeated verbatim on every page — the realistic shape for any
  multi-page PDF export (camelot returns one Table per page). Exercises
  core.parsing._glue_pdf_page_grids()'s "confirmed repeating header"
  branch: page 1 kept whole, the duplicate header dropped from pages 2-3.

Both PDF fixtures use real Cyrillic text and reuse the same realistic
product names as the Excel/CSV fixtures above. Generating them required
explicitly registering a Cyrillic-capable TTF font first — reportlab's
default Helvetica has NO Cyrillic glyphs and silently turns Russian text
into garbage ("nnnnnnn") instead of raising, a real trap already hit once
in this project.

Run directly to (re)generate the files:
    python backend/tests/fixtures/generate_fixtures.py
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import openpyxl
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FIXTURES_DIR = Path(__file__).parent

# Common install locations for a Cyrillic-capable TTF, checked in order.
# Only needed at *generation* time (this script) — the committed .pdf
# fixtures it produces carry their own embedded font subset, so reading
# them back via camelot later (in tests, on the server, anywhere) needs no
# font on the reading machine at all.
_CYRILLIC_FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\arial.ttf"),  # Windows dev machine
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),  # Debian/Ubuntu server
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
]


def _register_cyrillic_font() -> str:
    for candidate in _CYRILLIC_FONT_CANDIDATES:
        if candidate.exists():
            pdfmetrics.registerFont(TTFont("CyrillicFont", str(candidate)))
            return "CyrillicFont"
    raise RuntimeError(
        "No Cyrillic-capable TTF font found on this machine — refusing to "
        "generate PDF fixtures, since reportlab's default Helvetica would "
        "silently mangle the Russian text instead of raising. Install one "
        "(e.g. `apt install fonts-dejavu-core` on the server) or add its "
        "path to _CYRILLIC_FONT_CANDIDATES above."
    )


def _write_xlsx(filename: str, rows: list[list]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(FIXTURES_DIR / filename)
    print(f"wrote {filename} ({len(rows)} rows)")


def make_decorative_header_1c() -> None:
    """.xlsx with a decorative report-title row before the real header —
    the upload/mapping screen's core reason to exist."""
    rows = [
        ["Отчёт по продажам ООО «Ромашка» — август 2026", "", "", ""],
        ["Артикул", "Товар", "Кол-во", "Сумма"],
        ["WB-20011", "хлеб белый нарезной", "40", "2400"],
        ["WB-20012", "хлеб бородинский", "25", "1875"],
        ["WB-20099", "молоко 3.2% 1л", "60", "5400"],
        ["WB-20140", "сыр российский 200г", "18", "6300"],
        ["WB-20201", "масло сливочное 180г", "22", "4840"],
        ["WB-20305", "яйцо куриное С1 10шт", "35", "3150"],
    ]
    _write_xlsx("decorative_header_1c.xlsx", rows)


def make_sales_export_cp1251() -> None:
    """cp1251-encoded, ';'-delimited CSV with 1C-style "1 234,56" numbers —
    space thousands separator, comma decimal. Written by hand-encoding the
    text to bytes so the fixture is unambiguous about exactly what
    encoding it carries (no dependency on the writer's own default)."""
    lines = [
        "Товар;Кол-во;Сумма",
        "хлеб белый нарезной;120;1 234,56",
        "молоко 3.2% 1л;300;12 456,78",
        "сыр российский 200г;85;45 678,90",
        "масло сливочное 180г;64;9 999,99",
        "яйцо куриное С1 10шт;150;3 500,00",
        "гречка ядрица 900г;95;7 125,55",
    ]
    text = "\r\n".join(lines) + "\r\n"
    data = text.encode("cp1251")
    path = FIXTURES_DIR / "sales_export_cp1251.csv"
    path.write_bytes(data)
    print(f"wrote sales_export_cp1251.csv ({len(lines) - 1} data rows, cp1251, {len(data)} bytes)")


def make_similar_names_case_whitespace() -> None:
    """Same real product written three ways: different case, trailing
    whitespace. No decorative row — header is row 0 — since this fixture
    is specifically about entity *identity*, not header detection.
    Documented finding (see module docstring): these are three distinct
    entities under the current exact-match grouping, not fuzzy-merged."""
    rows = [
        ["Товар", "Сумма"],
        ["Носки чёрные 42 ", "1500"],   # trailing space
        ["носки чёрные 42", "900"],      # lowercase, no trailing space
        ["НОСКИ ЧЕРНЫЕ 42", "600"],      # uppercase, "е" not "ё"
        ["ботинки зимние жен.", "32000"],
        ["шапка вязаная бел.", "8900"],
    ]
    _write_xlsx("similar_names_case_whitespace.xlsx", rows)


def make_large_fractional_sums() -> None:
    """Many rows of fractional-kopeck amounts. Total computed with Decimal
    at generation time and asserted by a test / documented here so any
    drift on the upload->assign->export path is caught, not assumed away."""
    values = [
        Decimal("1234.56"), Decimal("789.01"), Decimal("456.78"), Decimal("2222.22"),
        Decimal("99.99"), Decimal("10000.10"), Decimal("0.33"), Decimal("0.34"),
        Decimal("0.33"), Decimal("3333.33"), Decimal("5555.55"), Decimal("6666.66"),
        Decimal("777.77"), Decimal("888.88"), Decimal("1.11"), Decimal("2.22"),
    ]
    total = sum(values, Decimal("0"))
    rows = [["Товар", "Сумма"]]
    for i, v in enumerate(values, start=1):
        rows.append([f"товар №{i:02d}", str(v)])
    _write_xlsx("large_fractional_sums.xlsx", rows)
    print(f"  expected total (Decimal-summed): {total}")


def make_pdf_invoice_no_borders() -> None:
    """Single-page table, header + 1 data row, drawn as plain positioned
    text — no c.line()/c.rect() calls anywhere, so there are no ruled
    borders for a border-seeking parser to find. This is the shape
    README's benchmark used to show pdfplumber-default finds 0 rows and
    camelot needs flavor="stream" specifically."""
    font = _register_cyrillic_font()
    path = FIXTURES_DIR / "pdf_invoice_no_borders.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont(font, 11)

    col_x = [70, 300, 430]
    rows = [
        ["Товар", "Кол-во", "Сумма"],
        ["хлеб бородинский", "25", "1875"],
    ]
    y = 780
    for row in rows:
        for x, value in zip(col_x, row):
            c.drawString(x, y, value)
        y -= 22
    c.save()
    print(f"wrote pdf_invoice_no_borders.pdf ({len(rows) - 1} data row, no borders)")


def make_pdf_multipage_table() -> None:
    """One logical table split across 3 pages, header row repeated
    verbatim on every page — the realistic pattern for a multi-page PDF
    export (camelot hands back one Table per page). Total computed with
    Decimal at generation time, same convention as large_fractional_sums,
    so the glue test can assert against a known-correct number, not trust
    the code under test."""
    font = _register_cyrillic_font()
    path = FIXTURES_DIR / "pdf_multipage_table.pdf"

    header = ["Товар", "Кол-во", "Сумма"]
    pages_data = [
        [
            ["хлеб белый нарезной", "40", "2400"],
            ["хлеб бородинский", "25", "1875"],
            ["молоко 3.2% 1л", "60", "5400"],
            ["сыр российский 200г", "18", "6300"],
        ],
        [
            ["масло сливочное 180г", "22", "4840"],
            ["яйцо куриное С1 10шт", "35", "3150"],
            ["гречка ядрица 900г", "95", "7125.55"],
            ["ботинки зимние жен.", "1", "32000"],
        ],
        [
            ["шапка вязаная бел.", "1", "8900"],
            ["носки чёрные 42", "10", "1500"],
            ["сахар песок 1кг", "40", "2600"],
            ["мука пшеничная 2кг", "30", "3300"],
        ],
    ]

    col_x = [70, 300, 430]
    c = canvas.Canvas(str(path), pagesize=A4)
    for page_rows in pages_data:
        c.setFont(font, 11)  # font does not persist across showPage()
        y = 780
        for x, value in zip(col_x, header):
            c.drawString(x, y, value)
        y -= 22
        for row in page_rows:
            for x, value in zip(col_x, row):
                c.drawString(x, y, value)
            y -= 22
        c.showPage()
    c.save()

    total = sum((Decimal(row[2]) for page in pages_data for row in page), Decimal("0"))
    n_rows = sum(len(page) for page in pages_data)
    print(f"wrote pdf_multipage_table.pdf ({len(pages_data)} pages, {n_rows} data rows)")
    print(f"  expected total (Decimal-summed): {total}")


if __name__ == "__main__":
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    make_decorative_header_1c()
    make_sales_export_cp1251()
    make_similar_names_case_whitespace()
    make_large_fractional_sums()
    make_pdf_invoice_no_borders()
    make_pdf_multipage_table()
