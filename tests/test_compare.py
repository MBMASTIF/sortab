from decimal import Decimal

from core.compare import STATUS_MATCH, STATUS_MISMATCH, STATUS_ONLY_A, STATUS_ONLY_B, compare_entities


def test_compare_the_four_canonical_cases():
    """Exactly the scenario the task brief calls for, computed independently
    here (not trusting the implementation): one entity matches exactly, one
    has a real discrepancy, one is only in A, one is only in B."""
    entities_a = ["ООО Ромашка", "ИП Иванов", "ЗАО Вектор"]
    values_a = [Decimal("10000.00"), Decimal("5000.00"), Decimal("777.77")]

    entities_b = ["ООО Ромашка", "ИП Иванов", "ООО Заря"]
    values_b = [Decimal("10000.00"), Decimal("5500.00"), Decimal("300.00")]

    rows = compare_entities(entities_a, values_a, entities_b, values_b)
    by_entity = {r.entity: r for r in rows}

    assert len(rows) == 4

    assert by_entity["ООО Ромашка"].status == STATUS_MATCH
    assert by_entity["ООО Ромашка"].diff == Decimal("0.00")

    assert by_entity["ИП Иванов"].status == STATUS_MISMATCH
    assert by_entity["ИП Иванов"].sum_a == Decimal("5000.00")
    assert by_entity["ИП Иванов"].sum_b == Decimal("5500.00")
    assert by_entity["ИП Иванов"].diff == Decimal("500.00")  # sum_b - sum_a

    assert by_entity["ЗАО Вектор"].status == STATUS_ONLY_A
    assert by_entity["ЗАО Вектор"].sum_a == Decimal("777.77")
    assert by_entity["ЗАО Вектор"].sum_b is None

    assert by_entity["ООО Заря"].status == STATUS_ONLY_B
    assert by_entity["ООО Заря"].sum_b == Decimal("300.00")
    assert by_entity["ООО Заря"].sum_a is None


def test_compare_aggregates_duplicate_rows_within_one_file():
    entities_a = ["Товар X", "Товар X"]
    values_a = [Decimal("100"), Decimal("50")]
    entities_b = ["Товар X"]
    values_b = [Decimal("150")]

    rows = compare_entities(entities_a, values_a, entities_b, values_b)
    assert len(rows) == 1
    assert rows[0].status == STATUS_MATCH
    assert rows[0].sum_a == Decimal("150")


def test_compare_exact_match_ignores_case_and_whitespace():
    rows = compare_entities(
        ["  Ромашка  "], [Decimal("100")],
        ["ромашка"], [Decimal("100")],
    )
    assert len(rows) == 1
    assert rows[0].status == STATUS_MATCH


def test_compare_fuzzy_match_catches_a_typo():
    rows = compare_entities(
        ["ООО Первоуральский завод №5"], [Decimal("1000")],
        ["ООО Первоуральский завод N5"], [Decimal("1200")],
    )
    assert len(rows) == 1
    assert rows[0].status == STATUS_MISMATCH
    assert rows[0].diff == Decimal("200")


def test_compare_fuzzy_match_is_blocked_by_digits_not_cross_matched():
    """Two entities differing only in their embedded number must NOT be
    fuzzy-matched to each other — this is exactly what digit-blocking
    exists to prevent (README: blocking by extracted digits before
    comparing)."""
    rows = compare_entities(
        ["Накладная №100"], [Decimal("500")],
        ["Накладная №200"], [Decimal("500")],
    )
    statuses = {r.status for r in rows}
    assert statuses == {STATUS_ONLY_A, STATUS_ONLY_B}


def test_compare_empty_file_b_marks_everything_only_a():
    rows = compare_entities(["X", "Y"], [Decimal("1"), Decimal("2")], [], [])
    assert len(rows) == 2
    assert all(r.status == STATUS_ONLY_A for r in rows)


def test_compare_money_is_decimal_not_float():
    """0.1 + 0.2 style precision loss must not appear anywhere in the diff."""
    rows = compare_entities(
        ["X"], [Decimal("0.1")],
        ["X"], [Decimal("0.2")],
    )
    assert rows[0].diff == Decimal("0.1")
    assert isinstance(rows[0].diff, Decimal)
