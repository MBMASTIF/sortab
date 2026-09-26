from decimal import Decimal

import openpyxl
import polars as pl

from core.export import UNASSIGNED_LABEL, build_detail_sheet, build_summary_sheet, export_workbook
from core.reconcile import Row, rollup
from core.tree import TreeStore


def _sample_tree():
    store = TreeStore()
    store.add_group("clothes", "Одежда")
    store.add_group("mens", "Мужская", parent_id="clothes")
    store.add_group("socks", "Носки", parent_id="mens")
    store.add_group("underwear", "Бельё", parent_id="mens")
    store.assign_entity("носки чёрные", "socks")
    store.assign_entity("носки белые", "socks")
    store.assign_entity("трусы", "underwear")
    return store


def test_detail_sheet_adds_group_path_columns():
    store = _sample_tree()
    original = pl.DataFrame(
        {
            "Товар": ["носки чёрные", "носки белые", "трусы", "неизвестный товар"],
            "Сумма": [500, 300, 200, 999],
        }
    )

    detail = build_detail_sheet(original, "Товар", store.assignment, store.as_group_list())

    assert "Группа 1" in detail.columns
    assert "Группа 2" in detail.columns
    assert "Группа 3" in detail.columns
    row0 = detail.filter(pl.col("Товар") == "носки чёрные").row(0, named=True)
    assert row0["Группа 1"] == "Одежда"
    assert row0["Группа 2"] == "Мужская"
    assert row0["Группа 3"] == "Носки"


def test_detail_sheet_marks_unassigned_rows_explicitly():
    store = _sample_tree()
    original = pl.DataFrame({"Товар": ["неизвестный товар"], "Сумма": [999]})
    detail = build_detail_sheet(original, "Товар", store.assignment, store.as_group_list())
    assert detail.row(0, named=True)["Группа 1"] == UNASSIGNED_LABEL


def test_summary_sheet_matches_rollup_totals():
    store = _sample_tree()
    rows = [
        Row("носки чёрные", Decimal("500")),
        Row("носки белые", Decimal("300")),
        Row("трусы", Decimal("200")),
        Row("неизвестный товар", Decimal("999")),
    ]
    result = rollup(rows, store.assignment, store.as_group_list())
    summary = build_summary_sheet(result, store.as_group_list())

    as_dict = dict(zip(summary["Группа"].str.strip_chars().to_list(), summary["Сумма"].to_list()))
    assert as_dict["Носки"] == "800"
    assert as_dict["Мужская"] == "1000"
    assert as_dict["Одежда"] == "1000"
    assert as_dict[UNASSIGNED_LABEL] == "999"
    assert as_dict["ИТОГО"] == "1999"


def test_full_pipeline_exports_a_real_readable_xlsx(tmp_path):
    """End-to-end: file data -> tree -> rollup -> export -> read back with
    openpyxl and verify the actual written bytes, not just that no
    exception was raised."""
    store = _sample_tree()
    original = pl.DataFrame(
        {
            "Товар": ["носки чёрные", "носки белые", "трусы", "неизвестный товар"],
            "Сумма": [500, 300, 200, 999],
        }
    )
    rows = [Row(t, Decimal(str(s))) for t, s in zip(original["Товар"], original["Сумма"])]
    result = rollup(rows, store.assignment, store.as_group_list())

    detail_df = build_detail_sheet(original, "Товар", store.assignment, store.as_group_list())
    summary_df = build_summary_sheet(result, store.as_group_list())

    out_path = tmp_path / "result.xlsx"
    export_workbook(str(out_path), detail_df, summary_df)

    book = openpyxl.load_workbook(out_path)
    assert book.sheetnames == ["Детализация", "Итоги"]

    detail_sheet = book["Детализация"]
    header = [c.value for c in detail_sheet[1]]
    assert "Группа 3" in header

    summary_sheet = book["Итоги"]
    summary_values = [(row[0].value, row[1].value) for row in summary_sheet.iter_rows(min_row=2)]
    summary_dict = {k.strip(): v for k, v in summary_values}
    assert summary_dict["ИТОГО"] == "1999"
    assert summary_dict[UNASSIGNED_LABEL] == "999"
