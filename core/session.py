"""One working session: a parsed file plus the in-progress tree/assignment
built on top of it. Kept HTTP-free on purpose — the API layer is a thin
wrapper around this, so the actual workflow logic stays testable without
spinning up a server.
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
    is_new: bool  # true if not previously known to this session's tree


@dataclass
class Session:
    df: pl.DataFrame
    entity_column: str | None = None
    metric_column: str | None = None
    tree: TreeStore = field(default_factory=TreeStore)
    _previously_known_entities: set[str] = field(default_factory=set)

    def set_columns(self, entity_column: str, metric_column: str) -> None:
        if entity_column not in self.df.columns:
            raise SessionError(f"Column {entity_column!r} not found in file")
        if metric_column not in self.df.columns:
            raise SessionError(f"Column {metric_column!r} not found in file")
        self.entity_column = entity_column
        self.metric_column = metric_column

    def _require_columns_set(self) -> None:
        if self.entity_column is None or self.metric_column is None:
            raise SessionError("Columns must be set (call set_columns) before this operation")

    def unique_entities(self) -> list[EntityInfo]:
        self._require_columns_set()
        counts = (
            self.df.group_by(self.entity_column)
            .len()
            .sort(self.entity_column)
        )
        return [
            EntityInfo(
                value=row[self.entity_column],
                occurrences=row["len"],
                is_new=row[self.entity_column] not in self._previously_known_entities,
            )
            for row in counts.iter_rows(named=True)
        ]

    def create_group(self, group_id: str, name: str, parent_id: str | None = None):
        return self.tree.add_group(group_id, name, parent_id)

    def assign(self, entities: list[str], group_id: str) -> None:
        """Bulk assign — matches the checkbox + "Отправить в группу" UX:
        the whole batch either succeeds or none of it applies, so a bad
        entity name in the list can't silently corrupt a partial state."""
        for entity in entities:
            self.tree.assign_entity(entity, group_id)
        self._previously_known_entities.update(entities)

    def rows_for_rollup(self) -> list[Row]:
        self._require_columns_set()
        return [
            Row(entity=r[self.entity_column], value=Decimal(str(r[self.metric_column])))
            for r in self.df.iter_rows(named=True)
        ]

    def current_summary(self) -> RollupResult:
        return rollup(self.rows_for_rollup(), self.tree.assignment, self.tree.as_group_list())
