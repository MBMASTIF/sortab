"""Tests backend/consolidate.py against real files parsed through
core.parsing.parse_file_raw() — same standard as every other module in
this project: prove the numbers, don't trust the implementation.

Scenario mirrors README "Задача: консолидация": two invented monthly
sales reports ("Июнь"/"Июль") with the same column structure (Товар/
Клиент/Выручка) but each with its OWN decorative preamble length before
the real header — exactly the real 1C shape core/parsing.py's own
docstring already documents.
"""

from decimal import Decimal

import openpyxl
import polars as pl
import pytest

from backend.columns import finalize_pipeline_columns
from backend.consolidate import ConsolidationError, SourceSpec, consolidate_sources
from core.parsing import parse_file_raw


def _write_xlsx(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(path)


@pytest.fixture
def june_source(tmp_path):
    path = tmp_path / "june.xlsx"
    _write_xlsx(
        path,
        [
            ["Отчёт за Июнь 2026", "", ""],  # decorative row — header is at index 1
            ["Товар", "Клиент", "Выручка"],
            ["Молоко", "ООО Ромашка", "1234,56"],
            ["Хлеб", "ИП Иванов", "500"],
        ],
    )
    return parse_file_raw(path).df


@pytest.fixture
def july_source(tmp_path):
    path = tmp_path / "july.xlsx"
    _write_xlsx(
        path,
        [
            ["Товар", "Клиент", "Выручка"],  # no decorative row — header is at index 0
            ["Молоко", "ООО Ромашка", "1000"],
            ["Сыр", "ИП Петров", "2000"],
            ["Хлеб", "ИП Иванов", "300"],
        ],
    )
    return parse_file_raw(path).df


def test_consolidate_combines_rows_with_source_column(june_source, july_source):
    combined_raw = consolidate_sources(
        [
            SourceSpec(raw_df=june_source, header_row_index=1, label="Июнь"),
            SourceSpec(raw_df=july_source, header_row_index=0, label="Июль"),
        ]
    )

    # Shaped exactly like core.parsing.parse_file_raw()'s own output:
    # generic column names, real header text living in row 0 of the data.
    assert list(combined_raw.columns) == ["column_1", "column_2", "column_3", "column_4"]
    rows = combined_raw.rows()
    assert rows[0] == ("Товар", "Клиент", "Выручка", "Источник")
    assert combined_raw.height == 1 + 2 + 3  # header row + 2 June rows + 3 July rows


def test_consolidate_feeds_the_normal_pipeline_with_correct_totals(june_source, july_source):
    """End-to-end proof: hand the consolidated raw grid to the SAME
    finalize_pipeline_columns() the /api/session/{id}/columns endpoint
    calls for a normal single-file session, pick "Выручка" as the sum and
    "Источник" as a разбивка column, and check the total against a total
    computed by hand — not trusting the implementation's own arithmetic."""
    combined_raw = consolidate_sources(
        [
            SourceSpec(raw_df=june_source, header_row_index=1, label="Июнь"),
            SourceSpec(raw_df=july_source, header_row_index=0, label="Июль"),
        ]
    )

    data_df, categorized, metrics, dimensions = finalize_pipeline_columns(
        combined_raw,
        header_row_index=0,
        categorized_col_idx=[0],  # Товар
        metric_col_idx=[2],  # Выручка
        dimension_col_idx=[3],  # Источник
    )

    assert categorized == ["Товар"]
    assert metrics == ["Выручка"]
    assert dimensions == ["Источник"]
    assert data_df.height == 5

    expected_total = Decimal("1234.56") + Decimal("500") + Decimal("1000") + Decimal("2000") + Decimal("300")
    actual_total = sum((Decimal(str(v)) for v in data_df["Выручка"].to_list()), Decimal("0"))
    assert actual_total == expected_total

    june_total = sum(
        (Decimal(str(v)) for v in data_df.filter(pl.col("Источник") == "Июнь")["Выручка"].to_list()),
        Decimal("0"),
    )
    july_total = sum(
        (Decimal(str(v)) for v in data_df.filter(pl.col("Источник") == "Июль")["Выручка"].to_list()),
        Decimal("0"),
    )
    assert june_total == Decimal("1234.56") + Decimal("500")
    assert july_total == Decimal("1000") + Decimal("2000") + Decimal("300")
    assert june_total + july_total == expected_total


def test_consolidate_rejects_mismatched_column_count(june_source, tmp_path):
    path = tmp_path / "bad.xlsx"
    _write_xlsx(path, [["Товар", "Выручка"], ["Хлеб", "300"]])
    bad_source = parse_file_raw(path).df

    with pytest.raises(ConsolidationError, match="колонок"):
        consolidate_sources(
            [
                SourceSpec(raw_df=june_source, header_row_index=1, label="Июнь"),
                SourceSpec(raw_df=bad_source, header_row_index=0, label="Плохой файл"),
            ]
        )


def test_consolidate_rejects_mismatched_column_names(june_source, tmp_path):
    path = tmp_path / "bad.xlsx"
    _write_xlsx(path, [["Товар", "Покупатель", "Выручка"], ["Хлеб", "Кто-то", "300"]])
    bad_source = parse_file_raw(path).df

    with pytest.raises(ConsolidationError, match="Плохой файл"):
        consolidate_sources(
            [
                SourceSpec(raw_df=june_source, header_row_index=1, label="Июнь"),
                SourceSpec(raw_df=bad_source, header_row_index=0, label="Плохой файл"),
            ]
        )


def test_consolidate_rejects_empty_source_list():
    with pytest.raises(ConsolidationError):
        consolidate_sources([])


def test_consolidate_reports_bad_header_row_index_with_source_label(june_source):
    with pytest.raises(ConsolidationError, match="Июнь"):
        consolidate_sources([SourceSpec(raw_df=june_source, header_row_index=99, label="Июнь")])


def test_consolidate_avoids_collision_with_existing_source_column(tmp_path):
    """If a real report already has a column literally named "Источник",
    the new one must not silently overwrite it."""
    path_a = tmp_path / "a.xlsx"
    _write_xlsx(path_a, [["Товар", "Источник", "Выручка"], ["Хлеб", "Склад 1", "100"]])
    path_b = tmp_path / "b.xlsx"
    _write_xlsx(path_b, [["Товар", "Источник", "Выручка"], ["Молоко", "Склад 2", "200"]])

    combined_raw = consolidate_sources(
        [
            SourceSpec(raw_df=parse_file_raw(path_a).df, header_row_index=0, label="Файл A"),
            SourceSpec(raw_df=parse_file_raw(path_b).df, header_row_index=0, label="Файл B"),
        ]
    )
    header_row = combined_raw.row(0)
    assert "Источник (2)" in header_row
    assert header_row.count("Источник") == 1  # the original column is untouched
