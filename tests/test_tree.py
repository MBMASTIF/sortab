from decimal import Decimal

import pytest

from core.reconcile import Row, rollup
from core.tree import GroupNotEmptyError, NotALeafError, TreeError, TreeStore


def test_add_nested_groups_and_check_leaf_status():
    store = TreeStore()
    store.add_group("clothes", "Одежда")
    store.add_group("mens", "Мужская", parent_id="clothes")
    store.add_group("socks", "Носки", parent_id="mens")

    assert store.is_leaf("socks") is True
    assert store.is_leaf("mens") is False  # has a child (socks)
    assert store.is_leaf("clothes") is False  # has a child (mens)


def test_cannot_assign_entity_to_non_leaf_group():
    store = TreeStore()
    store.add_group("clothes", "Одежда")
    store.add_group("mens", "Мужская", parent_id="clothes")

    with pytest.raises(NotALeafError):
        store.assign_entity("носки чёрные", "clothes")


def test_can_assign_entity_to_leaf_group():
    store = TreeStore()
    store.add_group("socks", "Носки")
    store.assign_entity("носки чёрные", "socks")
    assert store.assignment["носки чёрные"][0].group_id == "socks"


def test_cannot_delete_group_with_children():
    store = TreeStore()
    store.add_group("clothes", "Одежда")
    store.add_group("mens", "Мужская", parent_id="clothes")

    with pytest.raises(GroupNotEmptyError):
        store.remove_group("clothes")


def test_cannot_delete_group_with_assigned_entities():
    store = TreeStore()
    store.add_group("socks", "Носки")
    store.assign_entity("носки чёрные", "socks")

    with pytest.raises(GroupNotEmptyError):
        store.remove_group("socks")


def test_can_delete_empty_leaf_group():
    store = TreeStore()
    store.add_group("socks", "Носки")
    store.remove_group("socks")
    assert "socks" not in store.groups


def test_assigning_to_group_that_gains_a_child_later_is_blocked():
    """A group that was a valid leaf when first used can later grow a
    subgroup — assignment must be re-checked at call time, not cached from
    whenever the group was created."""
    store = TreeStore()
    store.add_group("socks", "Носки")
    store.assign_entity("носки а", "socks")  # fine, socks is a leaf right now

    store.add_group("black", "Чёрные", parent_id="socks")  # socks is no longer a leaf

    with pytest.raises(NotALeafError):
        store.assign_entity("носки б", "socks")


def test_unassigned_pool_reflects_current_state():
    store = TreeStore()
    store.add_group("socks", "Носки")
    store.assign_entity("носки а", "socks")

    all_entities = ["носки а", "носки б", "шапка"]
    assert store.get_unassigned(all_entities) == ["носки б", "шапка"]


def test_new_entity_detection_ignores_previously_seen_but_now_unassigned():
    store = TreeStore()
    store.add_group("socks", "Носки")
    store.assign_entity("носки а", "socks")
    store.unassign_entity("носки а")  # user changed their mind

    previously_known = {"носки а"}  # still "known" even though unassigned now
    current = ["носки а", "совершенно новый товар"]

    new_ones = store.detect_new_entities(current, previously_known)
    assert new_ones == ["совершенно новый товар"]


def test_unknown_group_reference_raises_on_assign():
    store = TreeStore()
    with pytest.raises(TreeError):
        store.assign_entity("носки", "does-not-exist")


def test_tree_builds_a_valid_snapshot_that_reconcile_can_roll_up():
    """End-to-end: prove core.tree and core.reconcile actually compose,
    not just that each passes its own tests in isolation."""
    store = TreeStore()
    store.add_group("clothes", "Одежда")
    store.add_group("mens", "Мужская", parent_id="clothes")
    store.add_group("socks", "Носки", parent_id="mens")
    store.add_group("underwear", "Бельё", parent_id="mens")

    store.assign_entity("носки чёрные", "socks")
    store.assign_entity("носки белые", "socks")
    store.assign_entity("трусы", "underwear")

    rows = [
        Row("носки чёрные", Decimal("500")),
        Row("носки белые", Decimal("300")),
        Row("трусы", Decimal("200")),
        Row("неизвестный товар", Decimal("999")),  # left unassigned on purpose
    ]

    result = rollup(rows, store.assignment, store.as_group_list())

    assert result.rollup_totals["socks"] == Decimal("800")
    assert result.rollup_totals["mens"] == Decimal("1000")
    assert result.rollup_totals["clothes"] == Decimal("1000")
    assert result.unassigned_total == Decimal("999")
    assert result.grand_total(store.as_group_list()) == Decimal("1999")
