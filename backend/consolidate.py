"""Combines several already-parsed sources into one dataset BEFORE the main
categorization pipeline (column-role picking -> category trees -> сверка)
ever sees it -- каталог услуг п.2 (несколько источников/консолидация) и
п.5 (объединение листов одного файла), see README "Задача: консолидация".

Lives in backend/, not core/, for the same reason backend/columns.py does
(see its own docstring): it depends on promote_header_row(), which is a
column-index/raw-upload resolution concern tied to the HTTP upload flow,
not a reusable engine primitive like core/tree.py or core/reconcile.py.
core/ never imports from backend/ anywhere else in this project -- this
module keeps that direction intact by living on the backend side itself.

Each source can have its header on a DIFFERENT row -- decorative 1C
preambles vary in length between files/sheets (already true even within a
single file today, see core/parsing.py's own docstring) -- so header
promotion has to happen PER SOURCE, before concatenation. There is no
single header_row_index that would work for every source at once.

Architecture decision (deliberate, see README): rather than inventing a
new "already-headered" session state -- which would need a second code
path through backend/columns.py::finalize_pipeline_columns AND a second
UI screen in upload.html -- consolidate_sources() hands back its result in
exactly the same *raw, header-agnostic* shape
core.parsing.parse_file_raw() already returns: generic "column_N" names,
with the resolved real column names (plus the new "Источник" column)
sitting in row 0 as plain data. That lets the entire rest of the already-
tested pipeline run completely unchanged: guessHeaderRow() on the
frontend picks up the synthetic row 0 as the header (it is the only
fully-populated row, easily outscoring any real data row), and every step
after that -- the role-picker screen, finalize_pipeline_columns(),
category trees, сверка -- is the identical, already-tested single-file
flow. One new concept reuses two already-tested screens instead of adding
a third.

Every column (including the new "Источник" one) is cast to Utf8 before
the synthetic header row is glued back on top -- not a shortcut, but
exactly what already happens today on a single real upload whenever a raw
Excel column mixes a text header with otherwise-numeric data: Polars has
one dtype per column, so the presence of a text value anywhere in the raw
column already forces the WHOLE column to String, and
finalize_pipeline_columns()'s existing normalize_decimal_comma() step
already handles converting that back to a real number once the user
confirms which column is the metric. This function simply makes that same
well-tested behavior explicit and universal, since here EVERY source
physically gets a synthetic text header glued onto it.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from backend.columns import ColumnMappingError, promote_header_row

SOURCE_COLUMN_BASE_NAME = "Источник"


class ConsolidationError(ValueError):
    pass


@dataclass
class SourceSpec:
    """One source picked for consolidation: a raw (header-agnostic)
    DataFrame -- the same shape core.parsing.parse_file_raw() /
    core.hierarchical.detect_and_unflatten() already return -- plus the
    header row index the user picked for THIS source specifically, and
    the label that will populate its "Источник" cell."""

    raw_df: pl.DataFrame
    header_row_index: int
    label: str


def _pick_source_column_name(existing_columns: list[str]) -> str:
    """"Источник" is the default name for the new source-label column.
    Real report columns collide with common words often enough (a report
    could legitimately already have its own "Источник" column) that a
    silent overwrite would be a real bug, not a hypothetical -- so this
    picks a name that's actually free, the same defensive pattern
    core/export.py::_safe_sheet_name() already uses for Excel sheet
    names."""
    if SOURCE_COLUMN_BASE_NAME not in existing_columns:
        return SOURCE_COLUMN_BASE_NAME
    n = 2
    while f"{SOURCE_COLUMN_BASE_NAME} ({n})" in existing_columns:
        n += 1
    return f"{SOURCE_COLUMN_BASE_NAME} ({n})"


def consolidate_sources(sources: list[SourceSpec]) -> pl.DataFrame:
    """Validates that every source has the same column composition (same
    count, same set of names, after each one's own header row is
    promoted), concatenates them in the given order with a new "Источник"
    column added, and returns the result in the raw, header-agnostic shape
    described in this module's docstring -- ready to be dropped straight
    into a normal core.session.Session exactly like a single-file upload.

    Deliberately does NOT try to guess a compatible mapping when sources
    disagree (e.g. matching columns by position instead of name, or
    dropping columns that don't appear everywhere) -- see task brief: an
    unclear/mismatched structure must be a clear error, never a silent
    best-effort merge that could quietly corrupt a real report.
    """
    if not sources:
        raise ConsolidationError("Нет ни одного источника для объединения")

    promoted: list[tuple[pl.DataFrame, list[str]]] = []
    for source in sources:
        try:
            data_df, columns = promote_header_row(source.raw_df, source.header_row_index)
        except ColumnMappingError as exc:
            raise ConsolidationError(f"Источник «{source.label}»: {exc}") from exc
        promoted.append((data_df, columns))

    first_label = sources[0].label
    first_columns = promoted[0][1]
    first_set = set(first_columns)

    for (_, columns), source in zip(promoted[1:], sources[1:]):
        if len(columns) != len(first_columns):
            raise ConsolidationError(
                f"Источник «{source.label}» несовместим с «{first_label}»: "
                f"{len(columns)} колонок вместо {len(first_columns)}. "
                "Структура (набор колонок) должна совпадать у всех источников — "
                "проверьте, что для каждого источника выбрана правильная строка заголовка."
            )
        columns_set = set(columns)
        if columns_set != first_set:
            missing = sorted(first_set - columns_set)
            extra = sorted(columns_set - first_set)
            details = []
            if missing:
                details.append(f"не хватает колонок: {', '.join(missing)}")
            if extra:
                details.append(f"лишние колонки: {', '.join(extra)}")
            raise ConsolidationError(
                f"Источник «{source.label}» несовместим с «{first_label}» — {'; '.join(details)}."
            )

    source_column = _pick_source_column_name(first_columns)

    labeled_frames: list[pl.DataFrame] = []
    for (data_df, columns), source in zip(promoted, sources):
        reordered = data_df.select(first_columns)
        as_strings = reordered.select(
            [pl.col(c).cast(pl.Utf8, strict=False).alias(c) for c in first_columns]
        )
        with_source = as_strings.with_columns(pl.lit(source.label).alias(source_column))
        labeled_frames.append(with_source)

    combined = pl.concat(labeled_frames, how="vertical")

    final_columns = [*first_columns, source_column]
    header_row_df = pl.DataFrame({c: [c] for c in final_columns})
    raw_like = pl.concat([header_row_df, combined], how="vertical")

    generic_names = [f"column_{i + 1}" for i in range(len(final_columns))]
    raw_like.columns = generic_names
    return raw_like
