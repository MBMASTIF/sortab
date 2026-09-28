"""One working session: a parsed file plus the in-progress trees/assignments
built on top of it. Kept HTTP-free on purpose — the API layer is a thin
wrapper around this, so the actual workflow logic stays testable without
spinning up a server.

**Multiple independent category trees.** A session can categorize more than
one column at once (e.g. "Товар" AND "Клиент" in the same uploaded report),
each with its OWN independent core.tree.TreeStore — own groups, own
leaf-only assignment, own reconciliation. Columns the user does NOT want to
categorize but still wants to see as raw context (no tree, just passed
through — e.g. "Менеджер" as a breakdown axis) are `dimension_columns`,
unrelated to categorization.

**Multiple independent sum columns.** `metric_columns` is a list, not a
single column — the user can pick more than one numeric column as a "sum"
at once (e.g. both "Стоимость" in rubles AND "Вес" in kg on the same
report). Every tree is reconciled against EVERY metric independently: the
same reused, unmodified `core.reconcile.rollup()` is called once per
(categorized column, metric column) pair — exactly the same "call the
untouched single-X engine N times" pattern this module already established
for multiple trees, just crossed with a second axis. Nothing about
`rollup()`, `TreeStore`, or a single tree's own bookkeeping changes because
there's more than one metric; `rows_for_rollup()`/`current_summary()` below
just take which metric to build `Row.value` from.

core.tree.TreeStore and core.reconcile.rollup() are reused completely
unmodified here — a categorized column just gets its own TreeStore
instance, and rollup() is called once per tree against that tree's own
assignment. Nothing about the single-tree engine changed; this module only
orchestrates N independent copies of it.
"""

from dataclasses import dataclass, field
from decimal import Decimal

import polars as pl

from core.reconcile import Row, RollupResult, rollup
from core.tree import TreeStore


class SessionError(ValueError):
    pass


@dataclass
class EntityInfo:
    value: str
    occurrences: int
    is_new: bool  # true if not previously known to this column's tree


@dataclass
class Session:
    df: pl.DataFrame
    categorized_columns: list[str] = field(default_factory=list)
    metric_columns: list[str] = field(default_factory=list)
    dimension_columns: list[str] = field(default_factory=list)
    # One independent TreeStore per categorized column, keyed by column name.
    trees: dict[str, TreeStore] = field(default_factory=dict)
    # "Known" entities are tracked PER categorized column — a value being
    # previously assigned in the "Товар" tree says nothing about whether
    # the same string has ever been seen in the "Клиент" tree.
    _previously_known_entities: dict[str, set[str]] = field(default_factory=dict)

    def set_columns(
        self,
        categorized_columns: list[str],
        metric_columns: list[str],
        dimension_columns: list[str] | None = None,
    ) -> None:
        if not categorized_columns:
            raise SessionError("At least one column must be chosen to categorize")
        if not metric_columns:
            raise SessionError("At least one column must be chosen as a sum")
        for col in categorized_columns:
            if col not in self.df.columns:
                raise SessionError(f"Column {col!r} not found in file")
        for col in metric_columns:
            if col not in self.df.columns:
                raise SessionError(f"Column {col!r} not found in file")

        dims = list(dimension_columns or [])
        for col in dims:
            if col not in self.df.columns:
                raise SessionError(f"Column {col!r} not found in file")

        all_used = [*categorized_columns, *metric_columns, *dims]
        if len(set(all_used)) != len(all_used):
            raise SessionError(
                "A column can't be used in more than one role at once "
                "(categorize / sum / breakdown)"
            )

        self.categorized_columns = categorized_columns
        self.metric_columns = metric_columns
        self.dimension_columns = dims

        for col in categorized_columns:
            self.trees.setdefault(col, TreeStore())
        self._previously_known_entities = {
            col: self._previously_known_entities.get(col, set()) for col in categorized_columns
        }

    def _require_columns_set(self) -> None:
        if not self.categorized_columns or not self.metric_columns:
            raise SessionError("Columns must be set (call set_columns) before this operation")

    def _require_metric_column(self, metric_column: str) -> None:
        if metric_column not in self.metric_columns:
            raise SessionError(f"Column {metric_column!r} is not one of this session's sum columns")

    def _require_categorized_column(self, column: str) -> None:
        if column not in self.categorized_columns:
            raise SessionError(f"Column {column!r} is not a categorized column of this session")

    def tree_for(self, column: str) -> TreeStore:
        self._require_categorized_column(column)
        return self.trees[column]

    def unique_entities(self, column: str) -> list[EntityInfo]:
        self._require_columns_set()
        self._require_categorized_column(column)
        known = self._previously_known_entities.get(column, set())
        counts = self.df.group_by(column).len().sort(column)
        return [
            EntityInfo(
                value=row[column],
                occurrences=row["len"],
                is_new=row[column] not in known,
            )
            for row in counts.iter_rows(named=True)
        ]

    def create_group(self, column: str, group_id: str, name: str, parent_id: str | None = None):
        self._require_categorized_column(column)
        return self.trees[column].add_group(group_id, name, parent_id)

    def assign(self, column: str, entities: list[str], group_id: str) -> None:
        """Bulk assign — matches the checkbox + "Отправить в группу" UX:
        the whole batch either succeeds or none of it applies, so a bad
        entity name in the list can't silently corrupt a partial state."""
        self._require_categorized_column(column)
        tree = self.trees[column]
        for entity in entities:
            tree.assign_entity(entity, group_id)
        self._previously_known_entities.setdefault(column, set()).update(entities)

    def rows_for_rollup(self, column: str, metric_column: str) -> list[Row]:
        self._require_columns_set()
        self._require_categorized_column(column)
        self._require_metric_column(metric_column)
        return [
            Row(entity=r[column], value=Decimal(str(r[metric_column])))
            for r in self.df.iter_rows(named=True)
        ]

    def current_summary(self, column: str, metric_column: str) -> RollupResult:
        """Independent reconciliation for ONE (tree, metric) pair:
        rollup() is called with THIS column's own assignment/groups, using
        ONLY this one metric's values, against the full row set — "Итого =
        По группам + Не распределено" holds for every categorized column,
        for every metric, independently — a session with 2 trees and 2
        metrics reconciles as 4 completely separate calls into the same
        unmodified rollup(), never mixed."""
        tree = self.tree_for(column)
        return rollup(self.rows_for_rollup(column, metric_column), tree.assignment, tree.as_group_list())
