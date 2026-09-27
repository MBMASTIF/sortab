"""Wide-to-long reshape ("Развернуть сводную" in the Инструменты catalog —
see README "Каталог услуг" item 4 / "Фаза 3"). This is a genuinely thin
wrapper: Polars already has the exact operation we need natively
(`DataFrame.unpivot`, the modern name for what pandas calls `.melt`), so
there is no hand-rolled reshape loop here — the only real logic this module
owns is validating the two/two+ column-role split and normalizing
Russian-formatted numeric text in the value columns before they're stacked,
the same explicit, user-confirmed step core.parsing.normalize_decimal_comma
was built for in the main pipeline (see backend/columns.py::finalize_columns).
"""

from __future__ import annotations

import polars as pl

from core.parsing import normalize_decimal_comma

CATEGORY_COLUMN = "Категория"
VALUE_COLUMN = "Значение"


class UnpivotError(ValueError):
    pass


def unpivot_table(df: pl.DataFrame, id_columns: list[str], value_columns: list[str]) -> pl.DataFrame:
    """Reshapes `df` from wide (one row per entity, one column per
    period/category) to long (one row per entity+category+value) — the
    shape 1C/CRM importers expect.

    `id_columns` are kept as-is (e.g. "Товар"); `value_columns` are the
    columns being stacked (e.g. "Январь", "Февраль", "Март") into two new
    columns: CATEGORY_COLUMN (the original column name) and VALUE_COLUMN
    (its cell value). Requires at least 1 id column and at least 2 value
    columns — with 0-1 value columns there's nothing to unpivot (a single
    value column is already long-format).
    """
    if len(id_columns) < 1:
        raise UnpivotError("Нужна хотя бы одна колонка, которая остаётся как есть")
    if len(value_columns) < 2:
        raise UnpivotError("Нужно минимум две колонки, которые нужно развернуть")

    overlap = set(id_columns) & set(value_columns)
    if overlap:
        raise UnpivotError(
            f"Колонка не может одновременно оставаться как есть и разворачиваться: {sorted(overlap)}"
        )

    working = df
    for col in value_columns:
        # Same convention as finalize_columns: only normalize columns that
        # actually arrived as text — a column calamine/Polars already
        # parsed as numeric is left untouched, never re-guessed.
        if working[col].dtype == pl.Utf8:
            working = working.with_columns(normalize_decimal_comma(working, col).alias(col))

    return working.unpivot(
        index=id_columns,
        on=value_columns,
        variable_name=CATEGORY_COLUMN,
        value_name=VALUE_COLUMN,
    )
