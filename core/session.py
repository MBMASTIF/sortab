"""One working session: a parsed file plus the in-progress trees/assignments
built on top of it. Kept HTTP-free on purpose — the API layer is a thin
wrapper around this, so the actual workflow logic stays testable without
spinning up a server.

**Multiple independent category trees.** A session can categorize more than
one column at once (e.g. "Товар" AND "Клиент" in the same uploaded report),
each with its OWN independent core.tree.TreeStore — own groups, own
leaf-only assignment, own reconciliation. `metric_column` is singular: it's
the same money column looked at through however many different trees the
user builds. Columns the user does NOT want to categorize but still wants
to see as raw context (no tree, just passed through — e.g. "Менеджер" as a
breakdown axis) are `dimension_columns`, unrelated to categorization.

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
    metric_column: str | None = None
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
        metric_column: str,
        dimension_columns: list[str] | None = None,
    ) -> None:
        if not categorized_columns:
            raise SessionError("At least one column must be chosen to categorize")
        for col in categorized_columns:
            if col not in self.df.columns:
                raise SessionError(f"Column {col!r} not found in file")
        if metric_column not in self.df.columns:
            raise SessionError(f"Column {metric_column!r} not found in file")

        dims = list(dimension_columns or [])
        for col in dims:
            if col not in self.df.columns:
                raise SessionError(f"Column {col!r} not found in file")

        all_used = [*categorized_columns, metric_column, *dims]
        if len(set(all_used)) != len(all_used):
            raise SessionError(
                "A column can't be used in more than one role at once "
                "(categorize / sum / breakdown)"
            )

        self.categorized_columns = categorized_columns
        self.metric_column = metric_column
        self.dimension_columns = dims

        for col in categorized_columns:
            self.trees.setdefault(col, TreeStore())
        self._previously_known_entities = {
            col: self._previously_known_entities.get(col, set()) for col in categorized_columns
        }

    def _require_columns_set(self) -> None:
        if not self.categorized_columns or self.metric_column is None:
            raise SessionError("Columns must be set (call set_columns) before this operation")

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

    def rows_for_rollup(self, column: str) -> list[Row]:
        self._require_columns_set()
        self._require_categorized_column(column)
        return [
            Row(entity=r[column], value=Decimal(str(r[self.metric_column])))
            for r in self.df.iter_rows(named=True)
        ]

    def current_summary(self, column: str) -> RollupResult:
        """Independent reconciliation for ONE tree: rollup() is called
        with THIS column's own assignment/groups against the full row set
        — "Итого = По группам + Не распределено" holds for every
        categorized column on its own, regardless of how many other trees
        exist in this session."""
        tree = self.tree_for(column)
        return rollup(self.rows_for_rollup(column), tree.assignment, tree.as_group_list())
