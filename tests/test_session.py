from decimal import Decimal

import polars as pl
import pytest

from core.session import Session, SessionError


def _sample_df():
    return pl.DataFrame(
        {
            "Товар": ["носки чёрные", "носки чёрные", "трусы", "неизвестный товар"],
            "Сумма": [100, 200, 300, 999],
        }
    )


def test_set_columns_rejects_unknown_column_name():
    session = Session(df=_sample_df())
    with pytest.raises(SessionError):
        session.set_columns("НетТакойКолонки", "Сумма")


def test_unique_entities_counts_occurrences_and_flags_new():
    session = Session(df=_sample_df())
    session.set_columns("Товар", "Сумма")

    entities = {e.value: e for e in session.unique_entities()}
    assert entities["носки чёрные"].occurrences == 2
    assert entities["трусы"].occurrences == 1
    assert entities["носки чёрные"].is_new is True  # nothing assigned yet


def test_assigning_marks_entity_as_no_longer_new():
    session = Session(df=_sample_df())
    session.set_columns("Товар", "Сумма")
    session.create_group("socks", "Носки")
    session.assign(["носки чёрные"], "socks")

    entities = {e.value: e for e in session.unique_entities()}
    assert entities["носки чёрные"].is_new is False
    assert entities["трусы"].is_new is True  # untouched, still new


def test_full_session_workflow_produces_correct_summary():
    session = Session(df=_sample_df())
    session.set_columns("Товар", "Сумма")
    session.create_group("clothes", "Одежда")
    session.create_group("socks", "Носки", parent_id="clothes")
    session.create_group("underwear", "Бельё", parent_id="clothes")

    session.assign(["носки чёрные"], "socks")
    session.assign(["трусы"], "underwear")
    # "неизвестный товар" deliberately left unassigned

    result = session.current_summary()
    assert result.rollup_totals["socks"] == Decimal("300")  # 100 + 200, two occurrences
    assert result.rollup_totals["underwear"] == Decimal("300")
    assert result.rollup_totals["clothes"] == Decimal("600")
    assert result.unassigned_total == Decimal("999")
    assert result.grand_total(session.tree.as_group_list()) == Decimal("1599")


def test_bulk_assign_is_all_or_nothing_on_bad_entity():
    """If assign() is called with a target that isn't a valid leaf, no
    partial state should stick — matches the tree layer's own guarantee,
    checked again here at the session boundary."""
    session = Session(df=_sample_df())
    session.set_columns("Товар", "Сумма")
    session.create_group("clothes", "Одежда")
    session.create_group("socks", "Носки", parent_id="clothes")

    with pytest.raises(Exception):
        session.assign(["носки чёрные"], "clothes")  # clothes is not a leaf

    assert "носки чёрные" not in session.tree.assignment
