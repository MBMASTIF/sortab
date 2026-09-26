"""Core rollup/reconciliation logic for the category tree.

This is the arithmetic the whole trust model depends on: the sum of every
group's total, plus the "unassigned" bucket, must always equal the sum of
every row in the source file — exactly, not approximately. Money uses
Decimal throughout; float is never used for row values.
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Group:
    id: str
    parent_id: str | None
    name: str


@dataclass(frozen=True)
class Row:
    entity: str
    value: Decimal


@dataclass(frozen=True)
class Split:
    """One row can be divided across several groups (the "ножницы" tool)."""
    group_id: str
    fraction: Decimal


@dataclass
class RollupResult:
    direct_totals: dict[str, Decimal]
    rollup_totals: dict[str, Decimal]
    unassigned_total: Decimal
    unassigned_rows: list[Row]

    def grand_total(self, groups: list[Group]) -> Decimal:
        """Sum of only ROOT-level rollups + unassigned — children are
        already folded into their parent's rollup_totals, so summing every
        level here would double-count."""
        roots = [g.id for g in groups if g.parent_id is None]
        return sum((self.rollup_totals.get(gid, Decimal(0)) for gid in roots), Decimal(0)) + self.unassigned_total


def _validate_tree(groups: list[Group]) -> None:
    ids = {g.id for g in groups}
    if len(ids) != len(groups):
        raise ValueError("Duplicate group id in tree")
    for g in groups:
        if g.parent_id is not None and g.parent_id not in ids:
            raise ValueError(f"Group {g.id!r} has unknown parent {g.parent_id!r}")


def rollup(
    rows: list[Row],
    assignment: dict[str, list[Split]],
    groups: list[Group],
) -> RollupResult:
    """Roll up row values through the group tree.

    `assignment` maps a normalized entity value to one or more Splits whose
    fractions must sum to exactly 1 (validated here, not trusted blindly —
    a bad split is exactly the kind of silent data-loss bug the reconcile
    screen exists to catch).
    """
    _validate_tree(groups)
    group_ids = {g.id for g in groups}
    children: dict[str, list[str]] = defaultdict(list)
    for g in groups:
        if g.parent_id is not None:
            children[g.parent_id].append(g.id)

    direct_totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    unassigned_total = Decimal(0)
    unassigned_rows: list[Row] = []

    for row in rows:
        splits = assignment.get(row.entity)
        if not splits:
            unassigned_total += row.value
            unassigned_rows.append(row)
            continue

        fraction_sum = sum((s.fraction for s in splits), Decimal(0))
        if fraction_sum != Decimal(1):
            raise ValueError(
                f"Splits for {row.entity!r} sum to {fraction_sum}, not 1 — "
                "this row would silently lose or invent money if applied"
            )
        for split in splits:
            if split.group_id not in group_ids:
                raise ValueError(f"Split references unknown group {split.group_id!r}")
            direct_totals[split.group_id] += row.value * split.fraction

    # Roll children up into parents, deepest first, so a parent's rollup
    # already includes every descendant by the time we reach it.
    depth: dict[str, int] = {}

    def compute_depth(gid: str) -> int:
        if gid in depth:
            return depth[gid]
        g = next(g for g in groups if g.id == gid)
        d = 0 if g.parent_id is None else compute_depth(g.parent_id) + 1
        depth[gid] = d
        return d

    for g in groups:
        compute_depth(g.id)

    rollup_totals: dict[str, Decimal] = dict(direct_totals)
    for g in groups:
        rollup_totals.setdefault(g.id, Decimal(0))

    for gid in sorted(group_ids, key=lambda i: depth[i], reverse=True):
        for child_id in children[gid]:
            rollup_totals[gid] = rollup_totals.get(gid, Decimal(0)) + rollup_totals.get(child_id, Decimal(0))
        # note: direct_totals[gid] is already inside rollup_totals[gid]
        # from the dict(direct_totals) seed above.

    return RollupResult(
        direct_totals=dict(direct_totals),
        rollup_totals=rollup_totals,
        unassigned_total=unassigned_total,
        unassigned_rows=unassigned_rows,
    )
