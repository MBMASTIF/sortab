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


def finalize_pipeline_columns(
    raw_df: pl.DataFrame,
    header_row_index: int,
    categorized_col_idx: list[int],
    metric_col_idx: list[int],
    dimension_col_idx: list[int] | None = None,
) -> tuple[pl.DataFrame, list[str], list[str], list[str]]:
    """The main pipeline's column-mapping step: promotes the header row,
    resolves an arbitrary number of "categorize this" columns (each gets
    its own independent core.tree.TreeStore — see core/session.py), an
    arbitrary number of "sum" (metric) columns — e.g. both "Стоимость" and
    "Вес" reconciled independently, see core/session.py's own docstring —
    and an arbitrary number of "разбивка" (raw context, no tree) columns,
    then normalizes every money column exactly like finalize_columns()
    does for its one.

    Kept SEPARATE from finalize_columns() (below), which stays exactly as
    it was: the Compare Инструмент (core/compare.py via
    backend/main.py::_run_compare) only ever needs ONE entity + ONE metric
    column per file and has nothing to do with category trees — reusing
    (or worse, mutating) finalize_columns()'s single-entity/single-metric
    contract for this multi-column case would either break Compare or
    force an unnatural role encoding onto a function that was never about
    that.

    Rows are dropped only when EVERY metric value is null after
    normalization (typically a blank trailing row after the real data) —
    NOT when any single metric is null, since a real report can
    legitimately have one metric filled in and another blank on the same
    row (e.g. "Вес" not tracked for every item) without that row being
    garbage.
    """
    data_df, new_columns = promote_header_row(raw_df, header_row_index)
    ncols = len(new_columns)

    if not categorized_col_idx:
        raise ColumnMappingError("At least one column must be chosen to categorize")
    if not metric_col_idx:
        raise ColumnMappingError("At least one column must be chosen as a sum")

    dims = dimension_col_idx or []
    all_idx = [*categorized_col_idx, *metric_col_idx, *dims]
    for idx in all_idx:
        if not (0 <= idx < ncols):
            raise ColumnMappingError(f"column index {idx} is out of range (0..{ncols - 1})")
    if len(set(all_idx)) != len(all_idx):
        raise ColumnMappingError(
            "A column index can't be used for more than one role (categorize / sum / разбивка) at once"
        )

    categorized_names = [new_columns[i] for i in categorized_col_idx]
    metric_names = [new_columns[i] for i in metric_col_idx]
    dimension_names = [new_columns[i] for i in dims]

    for metric_name in metric_names:
        if data_df[metric_name].dtype == pl.Utf8:
            normalized = normalize_decimal_comma(data_df, metric_name)
            data_df = data_df.with_columns(normalized.alias(metric_name))

    all_metrics_null = pl.all_horizontal([pl.col(m).is_null() for m in metric_names])
    data_df = data_df.filter(~all_metrics_null)

    return data_df, categorized_names, metric_names, dimension_names


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
