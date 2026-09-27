"""Turns a raw, header-agnostic DataFrame (from core.parsing.parse_file_raw)
into a properly-columned DataFrame once the user has confirmed, on the
upload/mapping screen, which row is the real header and which columns to
group/sum by.

Kept separate from backend/main.py so this logic — arguably the trickiest
part of the whole upload flow — is unit-testable without going through
HTTP, the same "keep it HTTP-free and testable" principle core/session.py
already follows.
"""

from __future__ import annotations

import polars as pl

from core.parsing import normalize_decimal_comma


class ColumnMappingError(ValueError):
    pass


def promote_header_row(raw_df: pl.DataFrame, header_row_index: int) -> tuple[pl.DataFrame, list[str]]:
    """Slices off everything up to and including the chosen header row and
    promotes that row's cell values to real column names (de-duplicated —
    "Сумма" appearing twice becomes "Сумма" and "Сумма (1)" — and never
    blank, falling back to "Колонка N").

    Shared by finalize_columns() (main pipeline: exactly one entity +
    one metric column) and the Инструменты column pickers (Unpivot needs
    an arbitrary number of id/value columns; Compare picks entity+metric
    per file, same as the main pipeline, via finalize_columns itself) —
    every caller needs this exact same header-promotion step first, only
    what happens to the columns AFTER naming them differs.
    """
    if not (0 <= header_row_index < raw_df.height):
        raise ColumnMappingError(
            f"header_row_index {header_row_index} is out of range (0..{raw_df.height - 1})"
        )

    header_values = raw_df.row(header_row_index)
    new_columns: list[str] = []
    seen: dict[str, int] = {}
    for i, value in enumerate(header_values):
        name = str(value).strip() if value is not None and str(value).strip() else f"Колонка {i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 0
        new_columns.append(name)

    data_df = raw_df.slice(header_row_index + 1)
    data_df.columns = new_columns
    return data_df, new_columns


def resolve_unpivot_columns(
    column_names: list[str], id_col_idx: list[int], value_col_idx: list[int]
) -> tuple[list[str], list[str]]:
    """Validates the Unpivot tool's column-index selection against the
    real (post-header-promotion) column count and turns indices into
    names. Kept here, not in core/unpivot.py, because index validation
    against a raw upload is an API-input concern the same way
    finalize_columns()'s index checks are — core.unpivot.unpivot_table()
    itself only ever sees real column names and stays HTTP-input-agnostic.
    """
    ncols = len(column_names)
    for idx in [*id_col_idx, *value_col_idx]:
        if not (0 <= idx < ncols):
            raise ColumnMappingError(f"column index {idx} is out of range (0..{ncols - 1})")

    if len(set(id_col_idx)) != len(id_col_idx):
        raise ColumnMappingError("id_columns contains duplicate indices")
    if len(set(value_col_idx)) != len(value_col_idx):
        raise ColumnMappingError("value_columns contains duplicate indices")

    overlap = set(id_col_idx) & set(value_col_idx)
    if overlap:
        raise ColumnMappingError(f"a column can't be both an id column and a value column: {sorted(overlap)}")

    id_names = [column_names[i] for i in id_col_idx]
    value_names = [column_names[i] for i in value_col_idx]
    return id_names, value_names


def finalize_columns(
    raw_df: pl.DataFrame,
    header_row_index: int,
    entity_col_idx: int,
    metric_col_idx: int,
) -> tuple[pl.DataFrame, str, str]:
    """Promotes the header row (see promote_header_row) and normalizes the
    chosen money column from Russian-formatted text ("1 234,56") into a
    real number — the same explicit, user-confirmed step
    core.parsing.normalize_decimal_comma() was built for (it's deliberately
    not auto-applied inside parse_file*, only once a human has confirmed
    this really is the money column).

    Rows with a null/blank entity value are dropped: a common real-world
    shape is a blank trailing row after the data, and letting it through
    would either blow up the rollup (Decimal(str(None)) raises) or
    silently create a bogus "None" category — neither is acceptable on
    the reconciliation screen this feeds.
    """
    data_df, new_columns = promote_header_row(raw_df, header_row_index)

    ncols = len(new_columns)
    if not (0 <= entity_col_idx < ncols):
        raise ColumnMappingError(f"entity_column index {entity_col_idx} is out of range (0..{ncols - 1})")
    if not (0 <= metric_col_idx < ncols):
        raise ColumnMappingError(f"metric_column index {metric_col_idx} is out of range (0..{ncols - 1})")
    if entity_col_idx == metric_col_idx:
        raise ColumnMappingError("entity_column and metric_column must be different columns")

    entity_col_name = new_columns[entity_col_idx]
    metric_col_name = new_columns[metric_col_idx]

    data_df = data_df.filter(pl.col(entity_col_name).is_not_null())

    if data_df[metric_col_name].dtype == pl.Utf8:
        normalized = normalize_decimal_comma(data_df, metric_col_name)
        data_df = data_df.with_columns(normalized.alias(metric_col_name))

    return data_df, entity_col_name, metric_col_name
