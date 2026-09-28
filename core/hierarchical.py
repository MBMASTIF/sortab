"""Detects and flattens a specific real-world 1C export shape: an
"expanded group tree" report (Менеджер -> Клиент -> Точка -> Товар, or any
similar N-level grouping) exported AS-IS from 1C's on-screen grouped view,
where every nesting level is a SEPARATE ROW in the same column (column A),
not a separate column — the level is encoded only in that cell's Excel
*indent* alignment attribute.

This is not a hypothesis — confirmed live against a real 4290-row 1C
export (see project brief): indent 0/2/4/6 mapped exactly to
Менеджер/Клиент/Точка/Товар, and the sum of every leaf (deepest-level) row
equalled the file's own "Итого" row to the penny once done in Decimal.

Why openpyxl, not python-calamine (calamine is this project's normal,
~21x-faster Excel reader — see README): calamine does not expose cell
STYLES at all, only values. `alignment.indent` is exactly a style
attribute, so this one detector is the deliberate, documented exception to
the calamine-first rule — it's only paid for Excel uploads, and only this
module pays it.

Detection is deliberately conservative: if the signal is ambiguous (a flat
file happens to have this module never even gets a chance to run without a
distinct-indent signal, or the leaf-sum can't be checked against a
recognized "Итого" row, or it doesn't check out to the penny) this module
returns None rather than guessing — the caller (backend/main.py's
/api/upload) falls straight back to the normal parse_file_raw() path and
the user picks columns by hand, same as any other file. Never half-forces
a wrong structure onto real money.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import openpyxl
import polars as pl

from core.parsing import ParsedFile

_TOTAL_ROW_RE = re.compile(r"^итого\b", re.IGNORECASE)

# openpyxl (the only Excel reader in this project that exposes cell
# STYLES, see module docstring) only opens the OOXML zip-based formats —
# legacy .xls (BIFF, a completely different binary format) and .xlsb
# (binary OOXML) are out of scope for this detector; those still go
# through the normal parse_file_raw() path untouched.
HIERARCHICAL_SUFFIXES = {".xlsx", ".xlsm"}


def _is_blank(value: object) -> bool:
    return value is None or str(value).strip() == ""


def _to_decimal(value: object) -> Decimal | None:
    """Best-effort Decimal parse of a raw openpyxl cell value — numeric
    cells come through as Python int/float already; text cells (a metric
    typed as text, or Russian decimal-comma formatting) are parsed the same
    tolerant way core/parsing.py::normalize_decimal_comma treats them.
    Returns None (not 0) for anything that isn't really a number, so a
    blank/label cell never silently counts as zero in a reconciliation."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if value is None:
        return None
    text = str(value).strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
    if text == "":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _open_workbook_robust(path: str | Path):
    """openpyxl.load_workbook(), with a fallback for a real bug already
    found (not hypothetical) in at least one real 1C export: the shared-
    strings part inside the .xlsx zip is named `xl/SharedStrings.xml`
    (capital S) instead of the OOXML-standard lowercase
    `xl/sharedStrings.xml`. openpyxl looks the part up by its exact
    lowercase name and raises KeyError('sharedStrings.xml') — this is a
    case-sensitivity mismatch in the zip's internal part names, not a
    corrupt file. Fixed by re-zipping the file with just that one entry
    renamed to the expected case, and ONLY after the normal open already
    failed with exactly this error — a well-formed workbook never takes
    this path at all."""
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
        return wb, None
    except KeyError as exc:
        if "sharedStrings" not in str(exc):
            raise

    fixed_path = _rezip_with_fixed_sharedstrings(path)
    wb = openpyxl.load_workbook(fixed_path, data_only=True)
    return wb, fixed_path


def _rezip_with_fixed_sharedstrings(path: str | Path) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            name = item.filename
            if name.lower() == "xl/sharedstrings.xml" and name != "xl/sharedStrings.xml":
                name = "xl/sharedStrings.xml"
            zout.writestr(name, data)
    return tmp.name


@dataclass
class _CellInfo:
    row_index: int
    value: object
    indent: int


def detect_and_unflatten(path: str | Path) -> ParsedFile | None:
    """Returns a ParsedFile shaped EXACTLY like core.parsing.parse_file_raw()
    already returns for a normal Excel file (generic "column_N" schema
    names, real header text sitting in row 0 of the DATA) — so the rest of
    the pipeline (backend/columns.py::promote_header_row, upload.html's
    "click the header row" screen) needs zero changes: the caller only has
    to pick this function's result over parse_file_raw() when it isn't
    None, everything downstream already works. Returns None whenever the
    file isn't confidently this exact hierarchical shape — see module
    docstring.
    """
    import os

    wb, tmp_fixed_path = _open_workbook_robust(path)
    try:
        ws = wb.active
        rows = list(ws.iter_rows())
        width = ws.max_column or 0
    finally:
        wb.close()
        if tmp_fixed_path is not None:
            os.unlink(tmp_fixed_path)

    if width < 2 or not rows:
        return None

    def cell_value(row_idx: int, col_idx: int):
        row = rows[row_idx]
        return row[col_idx].value if col_idx < len(row) else None

    col_a_cells: list[_CellInfo] = []
    for i, row in enumerate(rows):
        if not row:
            continue
        cell = row[0]
        value = cell.value
        if _is_blank(value):
            continue
        indent = int(cell.alignment.indent) if cell.alignment is not None and cell.alignment.indent else 0
        col_a_cells.append(_CellInfo(row_index=i, value=value, indent=indent))

    distinct_indents = sorted({c.indent for c in col_a_cells})
    if len(distinct_indents) <= 1:
        return None  # no indent signal at all — an ordinary flat table, not this shape

    leaf_indent = distinct_indents[-1]
    level_index_of_indent = {indent: idx for idx, indent in enumerate(distinct_indents)}
    n_levels = len(distinct_indents)

    total_row_idx = next(
        (c.row_index for c in col_a_cells if _TOTAL_ROW_RE.match(str(c.value).strip())), None
    )
    if total_row_idx is None:
        return None  # can't validate leaf-sum against a grand total — don't force a guess

    def row_metrics_blank(row_index: int) -> bool:
        return all(_is_blank(cell_value(row_index, j)) for j in range(1, width))

    # A genuine LEAF DATA row (not merely the first row that happens to sit
    # at the deepest indent — see below, that ambiguity is exactly why this
    # checks for actual metric values too, not just the indent).
    first_leaf_data_row = next(
        (c.row_index for c in col_a_cells if c.indent == leaf_indent and not row_metrics_blank(c.row_index)),
        None,
    )
    if first_leaf_data_row is None:
        return None  # no leaf-level row actually carries any metric data — nothing to unflatten

    # --- level captions ("Торговый агент"/"Контрагент"/...): best-effort,
    # never affects whether leaves are detected or sums reconcile — only
    # degrades the OUTPUT COLUMN NAMES to a fallback ("Уровень N") on a
    # miss. Deliberately NOT "first row at this indent with blank
    # metrics" — a real single-child chain (one client, one point, one
    # item under a manager) can legitimately produce consecutive rows at
    # 0/2/4/6 too, and its intermediate (non-leaf) rows legitimately have
    # blank metrics. What real data can NEVER produce is blank metrics on
    # EVERY level of that chain INCLUDING the leaf — a real leaf row is
    # the one place actual numbers live. So the signature this looks for
    # is a full, CONSECUTIVE (among non-blank column-A cells; filler blank
    # rows in between don't break it) run covering every distinct indent
    # level in order, with blank metrics on all of them — that shape is
    # the legend block 1C prints once near "Итого", not real tree data.
    caption_row_of_indent: dict[int, int] = {}
    caption_text_of_indent: dict[int, str] = {}
    for start in range(len(col_a_cells) - n_levels + 1):
        window = col_a_cells[start : start + n_levels]
        if [c.indent for c in window] != distinct_indents:
            continue
        if any(c.row_index == total_row_idx for c in window):
            continue
        if all(row_metrics_blank(c.row_index) for c in window):
            for c in window:
                caption_row_of_indent[c.indent] = c.row_index
                caption_text_of_indent[c.indent] = str(c.value).strip()
            break

    level_names = [
        caption_text_of_indent.get(indent, f"Уровень {idx + 1}") for idx, indent in enumerate(distinct_indents)
    ]

    metric_names = [f"Колонка {i}" for i in range(1, width)]
    for i in range(0, first_leaf_data_row):
        a_value = cell_value(i, 0)
        if not _is_blank(a_value):
            continue
        row_texts = [cell_value(i, j) for j in range(1, width)]
        has_text_header = any(
            (not _is_blank(t)) and not isinstance(t, (int, float)) for t in row_texts
        )
        if has_text_header:
            for j in range(1, width):
                t = cell_value(i, j)
                if not _is_blank(t):
                    metric_names[j - 1] = str(t).strip()
            break

    # --- walk every row top-to-bottom, tracking the "currently in effect"
    # value at each shallower level, emitting one output row per LEAF
    # (deepest-level) row. Subtotal rows (Итого, and any identified
    # caption row) never get emitted, and never corrupt the tracked
    # "current" state — see module docstring.
    excluded_rows = {total_row_idx, *caption_row_of_indent.values()}
    current: dict[int, str] = {}
    leaf_rows_out: list[tuple[list[str], list[object]]] = []
    for c in col_a_cells:
        if c.row_index in excluded_rows:
            continue
        level_idx = level_index_of_indent[c.indent]
        current[level_idx] = str(c.value).strip()
        if c.indent == leaf_indent:
            metric_values = [cell_value(c.row_index, j) for j in range(1, width)]
            level_values = [current.get(li, "") for li in range(n_levels)]
            leaf_rows_out.append((level_values, metric_values))

    if not leaf_rows_out:
        return None

    # --- confidence check: leaves must sum to the "Итого" row, exactly,
    # in Decimal, for AT LEAST one metric column with a real numeric total
    # to compare against, and must not MISMATCH on any column that could
    # be checked. This is the actual go/no-go gate for the whole detector.
    validated_any = False
    for j in range(1, width):
        total_value = _to_decimal(cell_value(total_row_idx, j))
        if total_value is None:
            continue
        leaf_sum = sum(
            (_to_decimal(metric_values[j - 1]) or Decimal(0) for _, metric_values in leaf_rows_out),
            Decimal(0),
        )
        if leaf_sum != total_value:
            return None
        validated_any = True

    if not validated_any:
        return None

    header_row = [*level_names, *metric_names]
    ncols = len(header_row)
    columns = [f"column_{i + 1}" for i in range(ncols)]
    schema = {c: pl.Utf8 for c in columns}

    data_rows: list[list[str]] = [header_row]
    for level_values, metric_values in leaf_rows_out:
        data_rows.append([*level_values, *[("" if v is None else str(v)) for v in metric_values]])

    df = pl.DataFrame(data_rows, schema=schema, orient="row")
    return ParsedFile(df=df, detected_encoding=None, detected_delimiter=None)
