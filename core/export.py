"""Builds the two-sheet output workbook: "Детализация" (original rows +
group-path breadcrumb + source, with an explicit "Не распределено" marker)
and "Итоги" (rollup per group, indented by depth, plus the unassigned
total) — the reconciliation screen made visible in the downloaded file,
not just on screen.

Deliberately built on small lookups joined onto the data (group tree is a
few dozen/hundred rows, never per-row Python loops) — this has to stay
fast even on 100k+ row files, matching the rest of the pipeline.
"""

from decimal import Decimal

import polars as pl
import xlsxwriter

from core.reconcile import Group, RollupResult

UNASSIGNED_LABEL = "Не распределено"


def _group_path(group_id: str, groups_by_id: dict[str, Group]) -> list[str]:
    """Root-to-leaf breadcrumb of group NAMES for one group id."""
    path: list[str] = []
    current: str | None = group_id
    seen: set[str] = set()
    while current is not None:
        if current in seen:
            raise ValueError(f"Cycle detected in group tree at {current!r}")
        seen.add(current)
        group = groups_by_id[current]
        path.append(group.name)
        current = group.parent_id
    return list(reversed(path))


def _max_depth(groups: list[Group]) -> int:
    groups_by_id = {g.id: g for g in groups}
    return max((len(_group_path(g.id, groups_by_id)) for g in groups), default=0)


def build_detail_sheet(
    original_df: pl.DataFrame,
    entity_column: str,
    assignment: dict[str, list],  # entity -> list[Split], only fraction=1 splits render sensibly here
    groups: list[Group],
    source_column: str | None = None,
) -> pl.DataFrame:
    groups_by_id = {g.id: g for g in groups}
    depth = _max_depth(groups)
    level_cols = [f"Группа {i + 1}" for i in range(depth)]

    # entity -> group_id (first split only; partial "ножницы" splits are a
    # Phase 3 concern and don't have a single clean path to show here yet)
    entity_to_group = {
        entity: splits[0].group_id for entity, splits in assignment.items() if splits
    }

    path_rows = []
    for group in groups:
        path = _group_path(group.id, groups_by_id)
        padded = path + [None] * (depth - len(path))
        path_rows.append((group.id, *padded))

    path_lookup = pl.DataFrame(
        path_rows, schema=["_group_id", *level_cols], orient="row"
    ) if path_rows else pl.DataFrame(schema={"_group_id": pl.Utf8, **{c: pl.Utf8 for c in level_cols}})

    entity_lookup = pl.DataFrame(
        {"_entity": list(entity_to_group.keys()), "_group_id": list(entity_to_group.values())}
    ) if entity_to_group else pl.DataFrame(schema={"_entity": pl.Utf8, "_group_id": pl.Utf8})

    result = (
        original_df
        .join(entity_lookup, left_on=entity_column, right_on="_entity", how="left")
        .join(path_lookup, on="_group_id", how="left")
        .drop("_group_id")
    )

    if level_cols:
        result = result.with_columns(pl.col(level_cols[0]).fill_null(UNASSIGNED_LABEL))
        for col in level_cols[1:]:
            result = result.with_columns(pl.col(col).fill_null(""))

    if source_column and source_column not in result.columns:
        raise ValueError(f"source_column {source_column!r} not found in original_df")

    return result


def build_summary_sheet(result: RollupResult, groups: list[Group]) -> pl.DataFrame:
    groups_by_id = {g.id: g for g in groups}
    rows = []
    for group in groups:
        depth = len(_group_path(group.id, groups_by_id))
        indent = "  " * (depth - 1)
        rows.append((f"{indent}{group.name}", str(result.rollup_totals.get(group.id, Decimal(0)))))

    rows.append((UNASSIGNED_LABEL, str(result.unassigned_total)))
    rows.append(("ИТОГО", str(result.grand_total(groups))))

    return pl.DataFrame(rows, schema=["Группа", "Сумма"], orient="row")


def export_workbook(
    path: str,
    detail_df: pl.DataFrame,
    summary_df: pl.DataFrame,
) -> None:
    wb = xlsxwriter.Workbook(path)
    detail_df.write_excel(workbook=wb, worksheet="Детализация")
    summary_df.write_excel(workbook=wb, worksheet="Итоги")
    wb.close()
