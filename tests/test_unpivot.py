from decimal import Decimal

import polars as pl
import pytest

from core.unpivot import CATEGORY_COLUMN, VALUE_COLUMN, UnpivotError, unpivot_table


def _wide_df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "Товар": ["Носки", "Ботинки"],
            "Январь": ["1000,50", "2000"],
            "Февраль": ["1100", "2200"],
            "Март": ["1200", "2300"],
        }
    )


def test_unpivot_reshapes_wide_to_long():
    result = unpivot_table(_wide_df(), id_columns=["Товар"], value_columns=["Январь", "Февраль", "Март"])

    assert set(result.columns) == {"Товар", CATEGORY_COLUMN, VALUE_COLUMN}
    assert result.height == 6  # 2 products x 3 months

    noski_jan = result.filter((pl.col("Товар") == "Носки") & (pl.col(CATEGORY_COLUMN) == "Январь"))
    assert noski_jan[VALUE_COLUMN][0] == pytest.approx(1000.50)

    boots_mar = result.filter((pl.col("Товар") == "Ботинки") & (pl.col(CATEGORY_COLUMN) == "Март"))
    assert boots_mar[VALUE_COLUMN][0] == pytest.approx(2300)


def test_unpivot_preserves_total_sum():
    """The whole point of the tool: every value from the wide table must
    survive into the long table, none lost or duplicated."""
    df = _wide_df()
    result = unpivot_table(df, id_columns=["Товар"], value_columns=["Январь", "Февраль", "Март"])

    expected_total = sum(
        Decimal(str(v).replace(",", ".")) for col in ["Январь", "Февраль", "Март"] for v in df[col].to_list()
    )
    actual_total = sum((Decimal(str(v)) for v in result[VALUE_COLUMN].to_list()), Decimal(0))
    assert actual_total == expected_total


def test_unpivot_supports_multiple_id_columns():
    df = pl.DataFrame(
        {
            "Товар": ["Носки", "Ботинки"],
            "Склад": ["Москва", "СПб"],
            "Январь": [100, 200],
            "Февраль": [110, 220],
        }
    )
    result = unpivot_table(df, id_columns=["Товар", "Склад"], value_columns=["Январь", "Февраль"])
    assert set(result.columns) == {"Товар", "Склад", CATEGORY_COLUMN, VALUE_COLUMN}
    assert result.height == 4


def test_unpivot_rejects_no_id_columns():
    with pytest.raises(UnpivotError):
        unpivot_table(_wide_df(), id_columns=[], value_columns=["Январь", "Февраль"])


def test_unpivot_rejects_fewer_than_two_value_columns():
    with pytest.raises(UnpivotError):
        unpivot_table(_wide_df(), id_columns=["Товар"], value_columns=["Январь"])


def test_unpivot_rejects_overlapping_id_and_value_columns():
    with pytest.raises(UnpivotError):
        unpivot_table(_wide_df(), id_columns=["Товар", "Январь"], value_columns=["Январь", "Февраль"])
