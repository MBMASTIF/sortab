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


def _multi_column_df():
    """Same shape as a real 1С "Продажи" export: Менеджер/Клиент/Товар as
    independently-categorizable columns, Выручка as the one shared metric."""
    return pl.DataFrame(
        {
            "Менеджер": ["Иванов", "Иванов", "Петров", "Петров"],
            "Клиент": ["ООО Ромашка", "ООО Ромашка", "ИП Сидоров", "ООО Вектор"],
            "Товар": ["носки чёрные", "трусы", "носки чёрные", "шапка"],
            "Выручка": [100, 200, 300, 999],
        }
    )


def test_set_columns_rejects_unknown_column_name():
    session = Session(df=_sample_df())
    with pytest.raises(SessionError):
        session.set_columns(["НетТакойКолонки"], "Сумма")


def test_set_columns_requires_at_least_one_categorized_column():
    session = Session(df=_sample_df())
    with pytest.raises(SessionError):
        session.set_columns([], "Сумма")


def test_set_columns_rejects_a_column_used_in_two_roles():
    session = Session(df=_sample_df())
    with pytest.raises(SessionError):
        session.set_columns(["Товар"], "Сумма", dimension_columns=["Товар"])


def test_unique_entities_counts_occurrences_and_flags_new():
    session = Session(df=_sample_df())
    session.set_columns(["Товар"], "Сумма")

    entities = {e.value: e for e in session.unique_entities("Товар")}
    assert entities["носки чёрные"].occurrences == 2
    assert entities["трусы"].occurrences == 1
    assert entities["носки чёрные"].is_new is True  # nothing assigned yet


def test_unique_entities_rejects_a_non_categorized_column():
    session = Session(df=_sample_df())
    session.set_columns(["Товар"], "Сумма")
    with pytest.raises(SessionError):
        session.unique_entities("Сумма")


def test_assigning_marks_entity_as_no_longer_new():
    session = Session(df=_sample_df())
    session.set_columns(["Товар"], "Сумма")
    session.create_group("Товар", "socks", "Носки")
    session.assign("Товар", ["носки чёрные"], "socks")

    entities = {e.value: e for e in session.unique_entities("Товар")}
    assert entities["носки чёрные"].is_new is False
    assert entities["трусы"].is_new is True  # untouched, still new


def test_full_session_workflow_produces_correct_summary():
    session = Session(df=_sample_df())
    session.set_columns(["Товар"], "Сумма")
    session.create_group("Товар", "clothes", "Одежда")
    session.create_group("Товар", "socks", "Носки", parent_id="clothes")
    session.create_group("Товар", "underwear", "Бельё", parent_id="clothes")

    session.assign("Товар", ["носки чёрные"], "socks")
    session.assign("Товар", ["трусы"], "underwear")
    # "неизвестный товар" deliberately left unassigned

    result = session.current_summary("Товар")
    assert result.rollup_totals["socks"] == Decimal("300")  # 100 + 200, two occurrences
    assert result.rollup_totals["underwear"] == Decimal("300")
    assert result.rollup_totals["clothes"] == Decimal("600")
    assert result.unassigned_total == Decimal("999")
    assert result.grand_total(session.tree_for("Товар").as_group_list()) == Decimal("1599")


def test_bulk_assign_is_all_or_nothing_on_bad_entity():
    """If assign() is called with a target that isn't a valid leaf, no
    partial state should stick — matches the tree layer's own guarantee,
    checked again here at the session boundary."""
    session = Session(df=_sample_df())
    session.set_columns(["Товар"], "Сумма")
    session.create_group("Товар", "clothes", "Одежда")
    session.create_group("Товар", "socks", "Носки", parent_id="clothes")

    with pytest.raises(Exception):
        session.assign("Товар", ["носки чёрные"], "clothes")  # clothes is not a leaf

    assert "носки чёрные" not in session.tree_for("Товар").assignment


# ---------------------------------------------------------------------
# Multiple independent trees in one session — the core of this phase.
# ---------------------------------------------------------------------


def test_multiple_categorized_columns_get_independent_trees():
    session = Session(df=_multi_column_df())
    session.set_columns(["Товар", "Клиент"], "Выручка", dimension_columns=["Менеджер"])

    assert session.tree_for("Товар") is not session.tree_for("Клиент")

    session.create_group("Товар", "socks", "Носки")
    session.create_group("Клиент", "big", "Крупные")

    # A group id created in one tree must not leak into the other tree's
    # namespace — assigning "big" (a Клиент-tree group) inside the
    # Товар tree must fail, they're genuinely separate TreeStores.
    with pytest.raises(Exception):
        session.assign("Товар", ["носки чёрные"], "big")


def test_two_trees_reconcile_independently_to_the_same_file_total():
    session = Session(df=_multi_column_df())
    session.set_columns(["Товар", "Клиент"], "Выручка")
    file_total = Decimal("100") + Decimal("200") + Decimal("300") + Decimal("999")

    # Товар tree: fully categorized
    session.create_group("Товар", "socks", "Носки")
    session.create_group("Товар", "other", "Остальное")
    session.assign("Товар", ["носки чёрные"], "socks")
    session.assign("Товар", ["трусы", "шапка"], "other")

    tovar_result = session.current_summary("Товар")
    assert tovar_result.grand_total(session.tree_for("Товар").as_group_list()) == file_total
    assert tovar_result.unassigned_total == Decimal("0")

    # Клиент tree: deliberately left with one client unassigned, proving
    # each tree's "Итого = По группам + Не распределено" holds on its own,
    # independent of the Товар tree's state.
    session.create_group("Клиент", "big", "Крупные")
    session.assign("Клиент", ["ООО Ромашка"], "big")
    # "ИП Сидоров" (300) and "ООО Вектор" (999) left unassigned

    client_result = session.current_summary("Клиент")
    assert client_result.grand_total(session.tree_for("Клиент").as_group_list()) == file_total
    assert client_result.unassigned_total == Decimal("300") + Decimal("999")
    assert client_result.rollup_totals["big"] == Decimal("100") + Decimal("200")


def test_known_entities_are_tracked_per_column_not_globally():
    """The same string value ("Иванов" as a Товар name vs a Менеджер name,
    say) being known in one tree must not mark it known in another — this
    is the exact bug a shared/global known-set would introduce."""
    df = pl.DataFrame({"A": ["x", "y"], "B": ["x", "z"], "Сумма": [1, 2]})
    session = Session(df=df)
    session.set_columns(["A", "B"], "Сумма")
    session.create_group("A", "g", "Группа")
    session.assign("A", ["x"], "g")

    a_entities = {e.value: e for e in session.unique_entities("A")}
    b_entities = {e.value: e for e in session.unique_entities("B")}
    assert a_entities["x"].is_new is False
    assert b_entities["x"].is_new is True  # "x" in column B was never assigned in B's tree
