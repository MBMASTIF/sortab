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

Run directly to (re)generate the files:
    python backend/tests/fixtures/generate_fixtures.py
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import openpyxl

FIXTURES_DIR = Path(__file__).parent


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


if __name__ == "__main__":
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    make_decorative_header_1c()
    make_sales_export_cp1251()
    make_similar_names_case_whitespace()
    make_large_fractional_sums()
