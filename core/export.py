"""Builds the output workbook: "Детализация" (original rows + a group-path
breadcrumb, with an explicit "Не распределено" marker) and one "Итоги"
sheet per category tree (rollup per group, indented by depth, plus the
unassigned total) — the reconciliation screen made visible in the
downloaded file, not just on screen. A session can have MORE THAN ONE
independent category tree (see core/session.py) — build_detail_sheet() and
build_summary_sheet() below are still the single-tree building blocks
(reused unmodified, once per tree); build_multi_tree_detail_sheet() and
export_workbook()'s extra_summary_sheets/extra_breakdown_sheets params are
what let a caller (backend/main.py) combine several trees' output into one
workbook without duplicating the underlying per-tree logic.

Deliberately built on small lookups joined onto the data (group tree is a
few dozen/hundred rows, never per-row Python loops) — this has to stay
fast even on 100k+ row files, matching the rest of the pipeline.
"""

import re
from decimal import Decimal

import polars as pl
import xlsxwriter

from core.reconcile import Group, RollupResult

UNASSIGNED_LABEL = "Не распределено"

_EXCEL_SHEET_NAME_MAX = 31
_UNSAFE_SHEET_CHARS = re.compile(r'[\[\]:\*\?/\\]')


def _safe_sheet_name(name: str, used: set[str]) -> str:
    """Excel worksheet names: max 31 chars, no [ ] : * ? / \\ — a real 1C
    column name ("Менеджер.Физическое лицо") plus a "Разбивка — " prefix
    can easily exceed that, which would otherwise make xlsxwriter raise.
    Truncates safely and de-dupes against sheet names already used in this
    workbook (same "(2)"-suffix pattern already used elsewhere in this
    project for name collisions — see backend/columns.py::promote_header_row
    and backend/main.py's mailmerge zip entry naming)."""
    cleaned = _UNSAFE_SHEET_CHARS.sub("", name).strip() or "Лист"
    base = cleaned[:_EXCEL_SHEET_NAME_MAX]
    candidate = base
    n = 1
    while candidate in used:
        n += 1
        suffix = f" ({n})"
        candidate = base[: _EXCEL_SHEET_NAME_MAX - len(suffix)] + suffix
    used.add(candidate)
    return candidate


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


def build_multi_tree_detail_sheet(
    original_df: pl.DataFrame,
    categorized_columns: list[str],
    trees: dict,  # column name -> core.tree.TreeStore
    dimension_columns: list[str] | None = None,
) -> pl.DataFrame:
    """"Детализация" for a session with one or MORE independent category
    trees (see core/session.py): every original row, plus a root-to-leaf
    group-path breadcrumb PER categorized column, each tree's path columns
    prefixed with that column's own name ("Товар: Группа 1",
    "Клиент: Группа 1", ...) so two trees' paths never collide — plus
    dimension (raw context, no tree) columns pulled to the front.

    Computes each tree's path via build_detail_sheet() itself (called once
    per tree, unmodified) rather than reimplementing the join — a left
    join keyed on unique entity/group-id lookups preserves original_df's
    row order and count exactly, so the new columns from each per-tree
    call can be hstacked back on by position, no join key needed for that
    second step.
    """
    result = original_df
    for column in categorized_columns:
        tree = trees[column]
        per_tree = build_detail_sheet(original_df, column, tree.assignment, tree.as_group_list())
        new_level_cols = [c for c in per_tree.columns if c not in original_df.columns]
        if not new_level_cols:
            continue  # this tree has no groups yet — nothing to add
        renamed = per_tree.select(new_level_cols).rename({c: f"{column}: {c}" for c in new_level_cols})
        result = pl.concat([result, renamed], how="horizontal_extend")

    dims = dimension_columns or []
    if dims:
        leading = [c for c in dims if c in result.columns]
        remaining = [c for c in result.columns if c not in leading]
        result = result.select([*leading, *remaining])

    return result


def build_breakdown_sheet(
    original_df: pl.DataFrame,
    entity_column: str,
    metric_column: str,
    dimension_columns: list[str],
    assignment: dict[str, list],
    groups: list[Group],
) -> pl.DataFrame:
    """Разбивка: sums the metric per (dimension_columns..., group path)
    combination for ONE tree — e.g. Менеджер -> Клиент -> how much revenue
    landed in each product category for that manager's client. Purely an
    ADDITIONAL cut on top of the already-correct rollup
    (core/reconcile.py::rollup(), untouched by this function and by
    everything in this module) — never the source of truth for "Итого",
    just another view of the same assigned rows.

    Aggregation is native Polars group_by(...).agg(pl.sum(...)) — NOT a
    Python per-row loop — matching the pattern this project already uses
    for grouping operations (see core/session.py::unique_entities), for
    performance on real multi-thousand-row 1C exports. The one deliberate
    compromise: Polars sums metric_column as Float64 (see
    core/parsing.py::normalize_decimal_comma — the metric column is never
    a Decimal-dtype column in this project), so THIS SECONDARY breakdown's
    running sum happens in float, not Decimal. Every finished aggregate
    cell is converted through Decimal(str(...)) before leaving this
    function — the same "str round-trip" trick core/session.py's own
    rows_for_rollup() already uses for the exact same reason — so callers
    only ever see a Decimal string, never a raw float. For real
    accounting-scale data (thousands of rows, cent precision) float64's
    ~15-17 significant digits make the theoretical summation error
    unmeasurable in practice; the actual grand total that has to reconcile
    exactly — core/reconcile.py::rollup()'s per-row Decimal accumulation —
    is completely untouched by this function.
    """
    groups_by_id = {g.id: g for g in groups}
    depth = _max_depth(groups)
    level_cols = [f"Группа {i + 1}" for i in range(depth)]

    group_cols = [*dimension_columns, *level_cols]
    if len(set(group_cols)) != len(group_cols):
        raise ValueError(f"Duplicate column name across dimension_columns/group levels: {group_cols}")

    entity_to_group = {
        entity: splits[0].group_id for entity, splits in assignment.items() if splits
    }

    path_rows = []
    for group in groups:
        path = _group_path(group.id, groups_by_id)
        padded = path + [None] * (depth - len(path))
        path_rows.append((group.id, *padded))

    path_lookup = (
        pl.DataFrame(path_rows, schema=["_group_id", *level_cols], orient="row")
        if path_rows
        else pl.DataFrame(schema={"_group_id": pl.Utf8, **{c: pl.Utf8 for c in level_cols}})
    )
    entity_lookup = (
        pl.DataFrame({"_entity": list(entity_to_group.keys()), "_group_id": list(entity_to_group.values())})
        if entity_to_group
        else pl.DataFrame(schema={"_entity": pl.Utf8, "_group_id": pl.Utf8})
    )

    joined = (
        original_df
        .join(entity_lookup, left_on=entity_column, right_on="_entity", how="left")
        .join(path_lookup, on="_group_id", how="left")
        .drop("_group_id")
    )

    if level_cols:
        joined = joined.with_columns(pl.col(level_cols[0]).fill_null(UNASSIGNED_LABEL))
        for col in level_cols[1:]:
            joined = joined.with_columns(pl.col(col).fill_null(""))

    if group_cols:
        grouped = (
            joined
            .group_by(group_cols)
            .agg(pl.col(metric_column).sum().alias("__sum"))
            .sort(group_cols)
        )
    else:
        # No dimension columns AND no tree groups yet — degenerate case,
        # sum everything into a single row.
        grouped = joined.select(pl.col(metric_column).sum().alias("__sum"))

    sums = [str(Decimal(str(v))) if v is not None else "0" for v in grouped["__sum"].to_list()]
    grouped = grouped.drop("__sum").with_columns(pl.Series("Сумма", sums))
    return grouped


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
    breakdown_df: pl.DataFrame | None = None,
    extra_summary_sheets: dict[str, pl.DataFrame] | None = None,
    extra_breakdown_sheets: dict[str, pl.DataFrame] | None = None,
) -> None:
    """`summary_df`/`breakdown_df` are the FIRST (or only) tree's sheets,
    named "Итоги"/"Разбивка" exactly as before — a session with a single
    categorized column produces an identical workbook shape to before this
    module supported multiple trees. `extra_summary_sheets`/
    `extra_breakdown_sheets` (dict of sheet name -> DataFrame) are for any
    ADDITIONAL trees beyond the first, written under their own name (see
    backend/main.py's export endpoint — sheet names there are
    "Итоги — {column}" / "Разбивка — {column}")."""
    wb = xlsxwriter.Workbook(path)
    used_names: set[str] = set()
    detail_df.write_excel(workbook=wb, worksheet=_safe_sheet_name("Детализация", used_names))
    summary_df.write_excel(workbook=wb, worksheet=_safe_sheet_name("Итоги", used_names))
    for name, df in (extra_summary_sheets or {}).items():
        df.write_excel(workbook=wb, worksheet=_safe_sheet_name(name, used_names))
    if breakdown_df is not None:
        breakdown_df.write_excel(workbook=wb, worksheet=_safe_sheet_name("Разбивка", used_names))
    for name, df in (extra_breakdown_sheets or {}).items():
        df.write_excel(workbook=wb, worksheet=_safe_sheet_name(name, used_names))
    wb.close()


def export_single_sheet(path: str, df: pl.DataFrame, sheet_name: str) -> None:
    """One-sheet workbook — used by the Инструменты (Unpivot, Compare),
    which don't have the Project pipeline's Детализация/Итоги two-sheet
    shape (see backend/main.py's /api/tools/* endpoints): there's no group
    tree to roll up here, just one flat result table to hand back."""
    wb = xlsxwriter.Workbook(path)
    df.write_excel(workbook=wb, worksheet=sheet_name)
    wb.close()
