"""Tests for the rollup/reconciliation core.

The one invariant that must never break, under any tree shape or input:
    grand_total == sum of every row's value, exactly.
That's the whole trust model of the product — these tests exist to make
sure a future change can never silently violate it.
"""

from decimal import Decimal

import pytest

from core.reconcile import Group, Row, Split, rollup


def money(s: str) -> Decimal:
    return Decimal(s)


def test_flat_tree_single_level():
    groups = [Group("clothes", None, "Одежда"), Group("shoes", None, "Обувь")]
    rows = [
        Row("товар а", money("100.00")),
        Row("товар б", money("250.50")),
        Row("товар в", money("30.00")),
    ]
    assignment = {
        "товар а": [Split("clothes", money("1"))],
        "товар б": [Split("clothes", money("1"))],
        "товар в": [Split("shoes", money("1"))],
    }
    result = rollup(rows, assignment, groups)
    assert result.rollup_totals["clothes"] == money("350.50")
    assert result.rollup_totals["shoes"] == money("30.00")
    assert result.unassigned_total == money("0")
    assert result.grand_total(groups) == sum((r.value for r in rows), Decimal(0))


def test_unassigned_rows_are_never_lost():
    groups = [Group("clothes", None, "Одежда")]
    rows = [
        Row("товар а", money("100")),
        Row("неизвестный товар", money("999.99")),
    ]
    assignment = {"товар а": [Split("clothes", money("1"))]}
    result = rollup(rows, assignment, groups)
    assert result.unassigned_total == money("999.99")
    assert len(result.unassigned_rows) == 1
    assert result.unassigned_rows[0].entity == "неизвестный товар"
    # Grand total must still include the unassigned money — this is the
    # exact number shown to the user as "Не распределено", it must never
    # silently vanish from the total.
    assert result.grand_total(groups) == money("1099.99")


def test_nested_tree_rolls_up_through_every_level():
    groups = [
        Group("clothes", None, "Одежда"),
        Group("mens", "clothes", "Мужская"),
        Group("socks", "mens", "Носки"),
        Group("underwear", "mens", "Бельё"),
    ]
    rows = [
        Row("носки чёрные", money("500")),
        Row("носки белые", money("300")),
        Row("трусы", money("200")),
    ]
    assignment = {
        "носки чёрные": [Split("socks", money("1"))],
        "носки белые": [Split("socks", money("1"))],
        "трусы": [Split("underwear", money("1"))],
    }
    result = rollup(rows, assignment, groups)
    assert result.rollup_totals["socks"] == money("800")
    assert result.rollup_totals["underwear"] == money("200")
    # mens must equal the sum of BOTH its children, not just direct rows
    # (mens has zero rows assigned directly to it)
    assert result.rollup_totals["mens"] == money("1000")
    assert result.rollup_totals["clothes"] == money("1000")
    assert result.grand_total(groups) == money("1000")


def test_scissors_split_across_two_groups_sums_exactly():
    groups = [Group("socks", None, "Носки"), Group("underwear", None, "Бельё")]
    rows = [Row("комплект носки+трусы", money("1000"))]
    assignment = {
        "комплект носки+трусы": [
            Split("socks", money("0.6")),
            Split("underwear", money("0.4")),
        ]
    }
    result = rollup(rows, assignment, groups)
    assert result.rollup_totals["socks"] == money("600.0")
    assert result.rollup_totals["underwear"] == money("400.0")
    assert result.grand_total(groups) == money("1000.0")


def test_scissors_split_not_summing_to_one_raises_instead_of_silently_losing_money():
    groups = [Group("socks", None, "Носки"), Group("underwear", None, "Бельё")]
    rows = [Row("комплект", money("1000"))]
    # 0.6 + 0.3 = 0.9, not 1 — this must be a loud error, not a quiet
    # 100-ruble leak out of the total.
    assignment = {
        "комплект": [Split("socks", money("0.6")), Split("underwear", money("0.3"))]
    }
    with pytest.raises(ValueError, match="sum to"):
        rollup(rows, assignment, groups)


def test_unknown_group_reference_raises():
    groups = [Group("socks", None, "Носки")]
    rows = [Row("носки", money("100"))]
    assignment = {"носки": [Split("does-not-exist", money("1"))]}
    with pytest.raises(ValueError, match="unknown group"):
        rollup(rows, assignment, groups)


def test_duplicate_group_id_raises():
    groups = [Group("a", None, "A"), Group("a", None, "A duplicate")]
    with pytest.raises(ValueError, match="Duplicate"):
        rollup([], {}, groups)


def test_group_with_unknown_parent_raises():
    groups = [Group("child", "no-such-parent", "Child")]
    with pytest.raises(ValueError, match="unknown parent"):
        rollup([], {}, groups)


def test_empty_file_reconciles_to_zero():
    groups = [Group("a", None, "A")]
    result = rollup([], {}, groups)
    assert result.grand_total(groups) == Decimal(0)


def test_decimal_precision_survives_many_small_fractions():
    """Float would accumulate rounding error here; Decimal must not."""
    groups = [Group("a", None, "A")]
    rows = [Row("x", money("0.10")) for _ in range(1000)]
    assignment = {"x": [Split("a", money("1"))]}
    result = rollup(rows, assignment, groups)
    assert result.rollup_totals["a"] == money("100.00")


@pytest.mark.parametrize("seed", range(20))
def test_property_grand_total_matches_row_sum_for_random_trees(seed):
    """Property-style check: for many random tree shapes and random
    assignments (including some rows left unassigned), the invariant must
    hold every time, not just in the hand-picked cases above."""
    import random

    rng = random.Random(seed)
    n_groups = rng.randint(1, 15)
    groups = [Group("g0", None, "root")]
    for i in range(1, n_groups):
        parent = rng.choice(groups).id
        groups.append(Group(f"g{i}", parent, f"group {i}"))

    rows = []
    assignment: dict[str, list[Split]] = {}
    for i in range(rng.randint(0, 50)):
        entity = f"item{i}"
        value = Decimal(rng.randint(1, 100000)) / Decimal(100)
        rows.append(Row(entity, value))
        if rng.random() < 0.15:
            continue  # leave unassigned on purpose
        target = rng.choice(groups).id
        assignment[entity] = [Split(target, Decimal(1))]

    result = rollup(rows, assignment, groups)
    expected = sum((r.value for r in rows), Decimal(0))
    assert result.grand_total(groups) == expected
