"""FastAPI HTTP layer wrapping the already-built, already-tested Python
engine in core/*.py. This module intentionally contains no business logic
of its own beyond request/response shaping and error-code mapping — every
real decision (rollup math, leaf-only assignment, destructive-delete
guards, file parsing) lives in core/ or backend/columns.py and is tested
there. See backend/tests/test_api.py for the end-to-end HTTP proof.

No authentication in this phase — sessions are anonymous and ephemeral by
design (see README "Ключевые решения"): a one-off file processing doesn't
need an account, only *saving* a Project will, and that's a later phase.
"""

from __future__ import annotations

import os
import re
import tempfile
import uuid
import zipfile
from decimal import Decimal
from pathlib import Path

import polars as pl
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from redis import Redis
from sqlalchemy.engine import Engine
from starlette.background import BackgroundTask

from backend.auth import (
    AUTH_COOKIE_NAME,
    AUTH_TOKEN_TTL_SECONDS,
    COOKIE_SECURE,
    create_auth_token,
    resolve_user_id,
)
from backend.batch_store import BatchNotFoundError, BatchSource, UploadBatchStore
from backend.columns import (
    ColumnMappingError,
    finalize_columns,
    finalize_pipeline_columns,
    promote_header_row,
    resolve_unpivot_columns,
)
from backend.consolidate import ConsolidationError, SourceSpec, consolidate_sources
from backend.db import get_engine
from backend.projects_store import (
    ProjectLimitError,
    ProjectNotFoundError,
    create_project,
    get_or_create_user,
    get_project,
    get_project_trees,
    list_projects,
)
from backend.session_store import (
    SessionEnvelope,
    SessionNotFoundError,
    get_redis_client,
    load_session,
    save_session,
)
from backend.tools_store import ToolFileStore, ToolTokenNotFoundError
from core.compare import compare_entities
from core.hierarchical import HIERARCHICAL_SUFFIXES, detect_and_unflatten
from core.export import (
    build_breakdown_sheet,
    build_detail_sheet,
    build_multi_tree_detail_sheet,
    build_summary_sheet,
    export_single_sheet,
    export_workbook,
)
from core.mailmerge import MailMergeError, build_row_dicts, render_pdf as render_mailmerge_pdf, render_text, validate_template
from core.parsing import UnsupportedFileError, list_excel_sheets, parse_file_raw
from core.pdf_split import SplitError, split_by_marker
from core.reconcile import RollupResult
from core.session import Session, SessionError
from core.tree import GroupNotEmptyError, NotALeafError, TreeError, TreeStore
from core.unpivot import UnpivotError, unpivot_table
from core.watermark import WatermarkError, add_watermark

app = FastAPI(title="gruper API")

PREVIEW_ROW_LIMIT = 20


def get_redis() -> Redis:
    """FastAPI dependency. Overridden in tests (dependency_overrides) with
    a fakeredis client so the suite needs no live Redis server."""
    return get_redis_client()


def get_db() -> Engine:
    """FastAPI dependency. Overridden in tests (dependency_overrides) with
    an in-memory SQLite engine, mirroring get_redis() above."""
    return get_engine()


def get_batch_store(redis_conn: Redis = Depends(get_redis)) -> UploadBatchStore:
    """FastAPI dependency for the multi-source upload batch store
    (backend/batch_store.py) — same Redis-connection-as-a-dependency
    pattern as get_tools_store() right below, for the identical
    multi-worker reason documented there."""
    return UploadBatchStore(redis_conn)


def get_tools_store(redis_conn: Redis = Depends(get_redis)) -> ToolFileStore:
    """FastAPI dependency for the Инструменты preview->commit token store
    (backend/tools_store.py) — built on the same Redis connection/override
    as get_redis() above, not a separate store, precisely so it works
    correctly across the deployed service's multiple uvicorn workers (see
    tools_store.py's docstring for why an earlier in-process version was
    wrong). Tests get this for free by overriding get_redis with
    fakeredis, same as every other Redis-backed endpoint in this file."""
    return ToolFileStore(redis_conn)


def require_user(request: Request, redis_conn: Redis = Depends(get_redis)) -> str:
    """Dependency for every /api/projects* endpoint: resolves the
    httpOnly cookie set by POST /api/auth/identify into a user_id, or
    raises a 401 with a message the frontend uses to trigger the "enter
    your email" prompt (see design-prototype/app.html)."""
    token = request.cookies.get(AUTH_COOKIE_NAME)
    user_id = resolve_user_id(redis_conn, token)
    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Нужно представиться по email, чтобы сохранять проекты — POST /api/auth/identify",
        )
    return user_id


class ColumnsRequest(BaseModel):
    header_row_index: int
    # One or more columns to build an independent category tree for (see
    # core/session.py — a session can categorize several columns at once,
    # e.g. "Товар" AND "Клиент" in the same report, each with its own tree).
    categorized_columns: list[int]
    # One or more numeric "sum" columns — e.g. both "Стоимость" and "Вес"
    # at once, each reconciled independently (see core/session.py).
    metric_columns: list[int]
    # Raw "разбивка" columns — no tree, just carried through as context
    # (e.g. "Менеджер"). Optional, any number, never overlaps with
    # categorized_columns or metric_columns.
    dimension_columns: list[int] = []


class BatchSourceChoice(BaseModel):
    """One entry of the consolidate request body — references a source
    from the batch BY INDEX (not by position in this list), so a source
    the user removed on the review screen can simply be left out, with no
    need to keep the request array's length in lockstep with the batch's."""

    index: int
    header_row_index: int
    label: str | None = None


class ConsolidateRequest(BaseModel):
    sources: list[BatchSourceChoice]


class CreateGroupRequest(BaseModel):
    group_id: str
    name: str
    parent_id: str | None = None


class AssignRequest(BaseModel):
    entity_ids: list[str]
    group_id: str


class IdentifyRequest(BaseModel):
    email: str


class CreateProjectRequest(BaseModel):
    session_id: str
    name: str


class UnpivotRequest(BaseModel):
    token: str
    header_row_index: int
    id_columns: list[int]
    value_columns: list[int]


class CompareRequest(BaseModel):
    token_a: str
    header_row_index_a: int
    entity_column_a: int
    metric_column_a: int
    token_b: str
    header_row_index_b: int
    entity_column_b: int
    metric_column_b: int


class MailMergeRequest(BaseModel):
    token: str
    header_row_index: int
    template: str


def _decimal_str(value: Decimal) -> str:
    return str(value)


def _translate_unsupported_file_error(message: str) -> str:
    """core.parsing.UnsupportedFileError's own text is English (dev/test
    facing — see its call sites), so this is the Russian text actually
    shown on the upload screen, following the same
    keep-the-domain-exception-English/translate-at-the-HTTP-boundary
    pattern already used below for core.tree's errors (see delete_group /
    assign_entities). Matched by substring rather than exception subclass
    because UnsupportedFileError is raised for several distinct reasons
    (bad extension, undetectable CSV encoding, unreadable/tableless PDF)
    that each need their own message, without a broader refactor of
    core/parsing.py's exception hierarchy."""
    lowered = message.lower()
    if "unsupported file extension" in lowered:
        return "Неподдерживаемый формат файла — загрузите Excel (.xlsx/.xls/.xlsm/.xlsb/.ods), CSV или PDF"
    if "encoding" in lowered:
        return "Не удалось определить кодировку CSV-файла — возможно, файл повреждён"
    if "pdf" in lowered:
        return "Не удалось прочитать PDF — возможно, файл повреждён или не содержит таблиц"
    return "Не удалось прочитать файл — проверьте формат и попробуйте снова"


def _load_or_404(redis_conn: Redis, session_id: str) -> SessionEnvelope:
    try:
        return load_session(redis_conn, session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _require_finalized(envelope: SessionEnvelope) -> None:
    if not envelope.finalized:
        raise HTTPException(
            status_code=409,
            detail="Колонки ещё не выбраны — сначала вызовите POST /api/session/{id}/columns",
        )


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), redis_conn: Redis = Depends(get_redis)):
    suffix = Path(file.filename or "").suffix.lower()
    contents = await file.read()

    tmp_path: str
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    parsed = None
    if suffix in HIERARCHICAL_SUFFIXES:
        # Opportunistic: a real 1C export can be an "expanded group tree"
        # report (see core/hierarchical.py) instead of a flat table —
        # detect_and_unflatten() is conservative and returns None for
        # anything it isn't confident about, so this never overrides the
        # normal flat-file path on a guess. Broad except deliberately —
        # this is a best-effort SECOND parse attempted before the normal
        # one; nothing about it should ever be able to fail an upload that
        # would otherwise have succeeded via parse_file_raw() below.
        try:
            parsed = detect_and_unflatten(tmp_path)
        except Exception:
            parsed = None

    if parsed is None:
        try:
            parsed = parse_file_raw(tmp_path)
        except UnsupportedFileError as exc:
            os.unlink(tmp_path)
            raise HTTPException(status_code=400, detail=_translate_unsupported_file_error(str(exc))) from exc

    os.unlink(tmp_path)

    session_id = str(uuid.uuid4())
    session = Session(df=parsed.df)
    save_session(redis_conn, session_id, session, finalized=False, header_row_index=None)

    preview_rows = [list(row) for row in parsed.df.head(PREVIEW_ROW_LIMIT).rows()]

    return {
        "session_id": session_id,
        "filename": file.filename,
        "columns_count": parsed.df.width,
        "row_count": parsed.df.height,
        "preview_rows": preview_rows,
        "detected_encoding": parsed.detected_encoding,
        "detected_delimiter": parsed.detected_delimiter,
    }


MAX_BATCH_SOURCES = 20


def _parse_one_batch_file(filename: str, tmp_path: str, suffix: str) -> list[BatchSource]:
    """Turns one uploaded file into one or more BatchSource entries: one
    per sheet for a multi-sheet Excel workbook ("объединение листов",
    каталог услуг п.5 — reuses the exact same multi-source mechanism п.2
    needs for multiple separate files, rather than a parallel ad-hoc code
    path — see backend/consolidate.py's module docstring), or a single
    entry for anything else (CSV/PDF/single-sheet Excel), matching how a
    plain POST /api/upload handles one file today."""
    try:
        sheet_names = list_excel_sheets(tmp_path)
    except Exception:
        sheet_names = None

    if sheet_names and len(sheet_names) > 1:
        sources = []
        for sheet in sheet_names:
            parsed = None
            if suffix in HIERARCHICAL_SUFFIXES:
                try:
                    parsed = detect_and_unflatten(tmp_path, sheet_name=sheet)
                except Exception:
                    parsed = None
            if parsed is None:
                try:
                    parsed = parse_file_raw(tmp_path, sheet_name=sheet)
                except UnsupportedFileError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"«{filename}», лист «{sheet}»: {_translate_unsupported_file_error(str(exc))}",
                    ) from exc
            sources.append(BatchSource(filename=f"{filename} — {sheet}", raw_df=parsed.df))
        return sources

    parsed = None
    if suffix in HIERARCHICAL_SUFFIXES:
        try:
            parsed = detect_and_unflatten(tmp_path)
        except Exception:
            parsed = None
    if parsed is None:
        try:
            parsed = parse_file_raw(tmp_path)
        except UnsupportedFileError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"«{filename}»: {_translate_unsupported_file_error(str(exc))}",
            ) from exc
    return [BatchSource(filename=filename or "файл", raw_df=parsed.df)]


@app.post("/api/upload/batch")
async def upload_batch(
    files: list[UploadFile] = File(...),
    batch_store: UploadBatchStore = Depends(get_batch_store),
):
    """First step of consolidating several sources ("несколько
    источников" / "объединение листов", каталог услуг пп. 2 и 5) into one
    dataset before the main pipeline's column-role picker ever runs — see
    backend/consolidate.py's module docstring for the full design.

    Parses every uploaded file the same way POST /api/upload does (same
    hierarchical-report opportunistic detection, same raw/header-agnostic
    shape), but does NOT create a Session yet — a Session needs ONE
    resolved header_row_index, and each source here can have its header on
    a different row, so that choice happens per source on the review
    screen this response feeds, not here. A multi-sheet Excel file expands
    into one source PER SHEET automatically (see _parse_one_batch_file).
    """
    if not files:
        raise HTTPException(status_code=400, detail="Нужно выбрать хотя бы один файл")

    batch_sources: list[BatchSource] = []
    for upload in files:
        suffix = Path(upload.filename or "").suffix.lower()
        contents = await upload.read()
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(contents)
            tmp_path = tmp.name
        try:
            batch_sources.extend(_parse_one_batch_file(upload.filename or "файл", tmp_path, suffix))
        finally:
            os.unlink(tmp_path)

    if len(batch_sources) > MAX_BATCH_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"Слишком много источников за один раз ({len(batch_sources)}) — максимум {MAX_BATCH_SOURCES}",
        )

    batch_id = batch_store.create(batch_sources)

    sources_payload = [
        {
            "index": i,
            "filename": source.filename,
            "columns_count": source.raw_df.width,
            "row_count": source.raw_df.height,
            "preview_rows": [list(row) for row in source.raw_df.head(PREVIEW_ROW_LIMIT).rows()],
        }
        for i, source in enumerate(batch_sources)
    ]

    return {"batch_id": batch_id, "sources": sources_payload}


@app.post("/api/upload/batch/{batch_id}/consolidate")
def consolidate_batch(
    batch_id: str,
    body: ConsolidateRequest,
    redis_conn: Redis = Depends(get_redis),
    batch_store: UploadBatchStore = Depends(get_batch_store),
):
    """Second step: the user has picked each kept source's header row and
    (optionally) renamed its label, and possibly dropped some sources —
    body.sources references batch entries BY INDEX, so a dropped source is
    simply absent, not padded with a placeholder. On success, returns the
    exact same response shape as POST /api/upload (session_id +
    preview_rows of a RAW, header-agnostic grid — see
    backend/consolidate.py) so upload.html's existing column-role-picker
    screen takes over completely unchanged."""
    try:
        batch_sources = batch_store.get(batch_id)
    except BatchNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="Загрузка источников не найдена или устарела — загрузите файлы заново",
        ) from exc

    if not body.sources:
        raise HTTPException(status_code=400, detail="Нужно оставить хотя бы один источник")

    specs: list[SourceSpec] = []
    for choice in body.sources:
        if not (0 <= choice.index < len(batch_sources)):
            raise HTTPException(
                status_code=400,
                detail=f"Источник с индексом {choice.index} не найден в этой загрузке",
            )
        batch_source = batch_sources[choice.index]
        label = (choice.label or batch_source.filename).strip() or batch_source.filename
        specs.append(SourceSpec(raw_df=batch_source.raw_df, header_row_index=choice.header_row_index, label=label))

    try:
        raw_like = consolidate_sources(specs)
    except ConsolidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    batch_store.delete(batch_id)

    session_id = str(uuid.uuid4())
    session = Session(df=raw_like)
    save_session(redis_conn, session_id, session, finalized=False, header_row_index=None)

    preview_rows = [list(row) for row in raw_like.head(PREVIEW_ROW_LIMIT).rows()]
    labels = [spec.label for spec in specs]
    display_name = labels[0] if len(labels) == 1 else f"{len(labels)} источников: {', '.join(labels)}"

    return {
        "session_id": session_id,
        "filename": display_name,
        "columns_count": raw_like.width,
        "row_count": raw_like.height,
        "preview_rows": preview_rows,
        "detected_encoding": None,
        "detected_delimiter": None,
    }


@app.post("/api/session/{session_id}/columns")
def set_columns(session_id: str, body: ColumnsRequest, redis_conn: Redis = Depends(get_redis)):
    envelope = _load_or_404(redis_conn, session_id)
    raw_df = envelope.session.df

    try:
        data_df, categorized_names, metric_names, dimension_names = finalize_pipeline_columns(
            raw_df, body.header_row_index, body.categorized_columns, body.metric_columns, body.dimension_columns
        )
    except ColumnMappingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session = Session(df=data_df)
    # Reuse whichever trees/known-entities already exist (by column name)
    # from the previous /columns call on this same session, so re-picking
    # columns mid-session doesn't silently discard in-progress work for a
    # column whose role didn't change.
    session.trees = {col: envelope.session.trees.get(col, TreeStore()) for col in categorized_names}
    session._previously_known_entities = dict(envelope.session._previously_known_entities)
    try:
        session.set_columns(categorized_names, metric_names, dimension_names)
    except SessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    save_session(redis_conn, session_id, session, finalized=True, header_row_index=body.header_row_index)

    return {
        "categorized_columns": categorized_names,
        "metric_columns": metric_names,
        "dimension_columns": dimension_names,
        "row_count": data_df.height,
    }


@app.get("/api/session/{session_id}/columns")
def get_columns(session_id: str, redis_conn: Redis = Depends(get_redis)):
    """Lets a client (design-prototype/app.html, after the upload.html ->
    app.html redirect) rediscover which columns this session categorizes
    — needed to build one tab per categorized column without smuggling
    that list through the URL."""
    envelope = _load_or_404(redis_conn, session_id)
    _require_finalized(envelope)
    return {
        "categorized_columns": envelope.session.categorized_columns,
        "metric_columns": envelope.session.metric_columns,
        "dimension_columns": envelope.session.dimension_columns,
    }


@app.get("/api/session/{session_id}/entities/{column}")
def get_entities(session_id: str, column: str, redis_conn: Redis = Depends(get_redis)):
    envelope = _load_or_404(redis_conn, session_id)
    _require_finalized(envelope)

    try:
        entities = envelope.session.unique_entities(column)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return [{"value": e.value, "occurrences": e.occurrences, "is_new": e.is_new} for e in entities]


@app.post("/api/session/{session_id}/groups/{column}", status_code=201)
def create_group(session_id: str, column: str, body: CreateGroupRequest, redis_conn: Redis = Depends(get_redis)):
    envelope = _load_or_404(redis_conn, session_id)

    try:
        group = envelope.session.create_group(column, body.group_id, body.name, body.parent_id)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TreeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    save_session(redis_conn, session_id, envelope.session, envelope.finalized, envelope.header_row_index)

    return {"id": group.id, "name": group.name, "parent_id": group.parent_id}


@app.get("/api/session/{session_id}/groups/{column}")
def get_groups(session_id: str, column: str, redis_conn: Redis = Depends(get_redis)):
    """Lets a client re-derive one column's group tree after a hard page
    reload — the tree lives in Redis via session.trees[column]. Each
    categorized column has its own independent tree (see core/session.py),
    so the client asks for one tab's tree at a time."""
    envelope = _load_or_404(redis_conn, session_id)
    try:
        tree = envelope.session.tree_for(column)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [{"id": g.id, "parent_id": g.parent_id, "name": g.name} for g in tree.as_group_list()]


@app.delete("/api/session/{session_id}/groups/{column}/{group_id}", status_code=204)
def delete_group(session_id: str, column: str, group_id: str, redis_conn: Redis = Depends(get_redis)):
    envelope = _load_or_404(redis_conn, session_id)

    try:
        tree = envelope.session.tree_for(column)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        tree.remove_group(group_id)
    except GroupNotEmptyError as exc:
        # A 409, not a bare 500 — expected, recoverable user action, not a
        # server fault. core.tree's own exception text is English (dev/test
        # facing); this is the Russian text actually shown to the user.
        raise HTTPException(
            status_code=409,
            detail="Нельзя удалить категорию: в ней ещё есть подкатегории или значения — сначала перенесите их",
        ) from exc
    except TreeError as exc:
        raise HTTPException(status_code=404, detail="Категория не найдена") from exc

    save_session(redis_conn, session_id, envelope.session, envelope.finalized, envelope.header_row_index)


@app.post("/api/session/{session_id}/assign/{column}")
def assign_entities(session_id: str, column: str, body: AssignRequest, redis_conn: Redis = Depends(get_redis)):
    envelope = _load_or_404(redis_conn, session_id)
    _require_finalized(envelope)

    try:
        envelope.session.assign(column, body.entity_ids, body.group_id)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NotALeafError as exc:
        raise HTTPException(
            status_code=409,
            detail="Нельзя распределить в эту категорию — в ней есть подкатегории, выберите одну из них",
        ) from exc
    except TreeError as exc:
        raise HTTPException(status_code=400, detail="Категория не найдена") from exc

    save_session(redis_conn, session_id, envelope.session, envelope.finalized, envelope.header_row_index)

    return {"assigned": len(body.entity_ids), "group_id": body.group_id, "column": column}


def _summary_json(result: RollupResult, groups: list) -> dict:
    return {
        "direct_totals": {k: _decimal_str(v) for k, v in result.direct_totals.items()},
        "rollup_totals": {k: _decimal_str(v) for k, v in result.rollup_totals.items()},
        "unassigned_total": _decimal_str(result.unassigned_total),
        # Real 1C exports can have genuinely blank entity cells (subtotal
        # rows, merged-cell artifacts) — entity is None for those. A plain
        # sorted() on a set mixing None and str raises TypeError (found live
        # on a real file: "'<' not supported between NoneType and str").
        # Sort key treats None as its own bucket instead of crashing.
        "unassigned_entities": sorted({r.entity for r in result.unassigned_rows}, key=lambda e: (e is None, e)),
        "grand_total": _decimal_str(result.grand_total(groups)),
    }


@app.get("/api/session/{session_id}/summary/{column}")
def get_summary(session_id: str, column: str, redis_conn: Redis = Depends(get_redis)):
    """Reconciliation for ONE tree, across EVERY chosen sum column — "Итого
    = По группам + Не распределено" holds independently per (categorized
    column, metric column) pair, since each tree partitions the SAME rows
    differently and each metric sums a different column of the SAME rows
    (see core/session.py::current_summary and its docstring). Response
    shape (decided this phase, see README): one summary PER metric, keyed
    by metric column name, under "metrics" — e.g.
    {"metrics": {"Стоимость": {...}, "Вес": {...}}} — rather than N
    separate HTTP calls, so the frontend can show every sum side by side
    for one tree/column in a single round trip."""
    envelope = _load_or_404(redis_conn, session_id)
    _require_finalized(envelope)
    session = envelope.session

    try:
        groups = session.tree_for(column).as_group_list()
        metrics = {
            metric: _summary_json(session.current_summary(column, metric), groups)
            for metric in session.metric_columns
        }
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {
        "column": column,
        "metric_columns": session.metric_columns,
        "metrics": metrics,
    }


@app.get("/api/session/{session_id}/breakdown/{column}")
def get_breakdown(session_id: str, column: str, redis_conn: Redis = Depends(get_redis)):
    """Разбивка: this column's tree's group path crossed with the
    session's raw "разбивка" (dimension) columns — e.g. Менеджер -> Клиент
    -> сумма per Товар category, for the Товар tree specifically. A purely
    additional cut; see core/export.py::build_breakdown_sheet for why this
    never touches the actual reconciliation math.

    With more than one sum column, build_breakdown_sheet() (unmodified,
    single-metric — see its own docstring) is called once per metric, same
    "call the untouched single-X function N times" pattern used everywhere
    else in this project for a second axis of multiplicity. The N
    single-metric tables always share the exact same group keys (same
    original_df/tree/assignment), so they're joined into ONE table here —
    one row per dimension/group combination, one column per metric, named
    by the bare metric name (e.g. "Стоимость", "Вес": guaranteed not to
    collide with a dimension/group column name, since set_columns() never
    lets the same column serve two roles at once) — so the frontend shows
    every sum side by side without N separate round trips or tables."""
    envelope = _load_or_404(redis_conn, session_id)
    _require_finalized(envelope)
    session = envelope.session

    try:
        tree = session.tree_for(column)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    merged: pl.DataFrame | None = None
    for metric in session.metric_columns:
        per_metric = build_breakdown_sheet(
            session.df,
            column,
            metric,
            session.dimension_columns,
            tree.assignment,
            tree.as_group_list(),
        ).rename({"Сумма": metric})
        if merged is None:
            merged = per_metric
        else:
            join_cols = [c for c in per_metric.columns if c != metric]
            merged = merged.join(per_metric, on=join_cols, how="left") if join_cols else pl.concat(
                [merged, per_metric], how="horizontal"
            )

    rows = merged.to_dicts() if merged is not None else []
    return {
        "column": column,
        "dimension_columns": session.dimension_columns,
        "metric_columns": session.metric_columns,
        "rows": rows,
    }


@app.get("/api/session/{session_id}/export")
def export_session(session_id: str, redis_conn: Redis = Depends(get_redis)):
    """The exported workbook now varies along TWO independent axes —
    categorized column (tree) and metric column (sum) — not just the one
    axis (tree) the file shape already supported. Same backward-
    compatibility rule as before, just extended to the new axis: a session
    with exactly one tree AND exactly one metric produces the EXACT same
    2-sheet "Детализация"/"Итоги"(+"Разбивка") file as before this phase
    (proven by test_export_single_tree_single_metric_unchanged in
    backend/tests/test_api.py). Every (tree, metric) pair beyond the very
    first gets its own sheet, named "Итоги"/"Разбивка" plus whichever of
    "— {column}" / "— {metric}" actually applies — e.g. a 2-tree,
    1-metric session still says "Итоги — Клиент" exactly as before; a
    1-tree, 2-metric session says "Итоги — Вес" (no column suffix, since
    there's only the one tree); a 2-tree, 2-metric session says
    "Итоги — Клиент — Вес" for the pair that's neither the first tree nor
    the first metric.
    """
    envelope = _load_or_404(redis_conn, session_id)
    _require_finalized(envelope)

    session = envelope.session
    first_col, *rest_cols = session.categorized_columns
    first_metric, *rest_metrics = session.metric_columns

    detail_df = build_multi_tree_detail_sheet(
        session.df, session.categorized_columns, session.trees, dimension_columns=session.dimension_columns
    )

    def sheet_suffix(col: str, metric: str) -> str | None:
        parts = []
        if col != first_col:
            parts.append(col)
        if metric != first_metric:
            parts.append(metric)
        return " — ".join(parts) if parts else None

    summary_df: pl.DataFrame | None = None
    breakdown_df: pl.DataFrame | None = None
    extra_summary_sheets: dict[str, pl.DataFrame] = {}
    extra_breakdown_sheets: dict[str, pl.DataFrame] = {}

    for col in session.categorized_columns:
        tree = session.trees[col]
        for metric in session.metric_columns:
            result = session.current_summary(col, metric)
            this_summary = build_summary_sheet(result, tree.as_group_list())
            this_breakdown = None
            if session.dimension_columns:
                this_breakdown = build_breakdown_sheet(
                    session.df, col, metric, session.dimension_columns, tree.assignment, tree.as_group_list(),
                )

            suffix = sheet_suffix(col, metric)
            if suffix is None:
                summary_df = this_summary
                breakdown_df = this_breakdown
            else:
                extra_summary_sheets[f"Итоги — {suffix}"] = this_summary
                if this_breakdown is not None:
                    extra_breakdown_sheets[f"Разбивка — {suffix}"] = this_breakdown

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    export_workbook(tmp.name, detail_df, summary_df, breakdown_df, extra_summary_sheets, extra_breakdown_sheets)

    return FileResponse(
        tmp.name,
        filename="gruper_export.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        # Delete the temp file only after it's fully streamed to the
        # client — deleting it inline would race the response.
        background=BackgroundTask(os.unlink, tmp.name),
    )


# ---------------------------------------------------------------------
# "Мягкий email" identification + Проекты (Postgres). See backend/auth.py
# and backend/projects_store.py for the actual logic — this section is
# just the same thin HTTP wrapping the rest of this file already does.
# ---------------------------------------------------------------------


@app.post("/api/auth/identify")
def identify(
    body: IdentifyRequest,
    response: Response,
    redis_conn: Redis = Depends(get_redis),
    engine: Engine = Depends(get_db),
):
    email = body.email.strip().lower()
    # Deliberately loose validation — this is a low-friction lead-capture
    # step, not account creation with a confirmation email (see README).
    # Just enough to reject obvious junk.
    if "@" not in email or len(email) < 3:
        raise HTTPException(status_code=400, detail="Некорректный email")

    user_id = get_or_create_user(engine, email)
    token = create_auth_token(redis_conn, user_id)
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=AUTH_TOKEN_TTL_SECONDS,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )
    return {"email": email}


@app.post("/api/projects", status_code=201)
def save_project(
    body: CreateProjectRequest,
    user_id: str = Depends(require_user),
    redis_conn: Redis = Depends(get_redis),
    engine: Engine = Depends(get_db),
):
    envelope = _load_or_404(redis_conn, body.session_id)
    _require_finalized(envelope)

    name = body.name.strip() or "Без названия"
    session = envelope.session
    try:
        return create_project(
            engine,
            user_id,
            name,
            session.categorized_columns,
            session.metric_columns,
            session.trees,
        )
    except ProjectLimitError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/projects")
def get_projects(user_id: str = Depends(require_user), engine: Engine = Depends(get_db)):
    return list_projects(engine, user_id)


@app.get("/api/projects/{project_id}")
def get_one_project(project_id: str, user_id: str = Depends(require_user), engine: Engine = Depends(get_db)):
    try:
        return get_project(engine, user_id, project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/session/{session_id}/load-project/{project_id}")
def load_project_into_session(
    session_id: str,
    project_id: str,
    user_id: str = Depends(require_user),
    redis_conn: Redis = Depends(get_redis),
    engine: Engine = Depends(get_db),
):
    """The actual point of the whole Projects feature: pull a saved set of
    tree(s)+dictionaries into a freshly-uploaded working session, BEFORE
    the user starts sorting anything, so only genuinely new entities need
    manual attention this time around. A Project can hold more than one
    independent tree (see backend/projects_store.py::get_project_trees) —
    each is matched back onto this session's tree for the SAME column
    name; a saved column this session's file doesn't have (or didn't
    choose to categorize) is skipped, not an error, so the rest of the
    project's dictionaries can still apply.

    Must run after /columns (the session needs its categorized columns
    resolved to know the current file's unique entity values per column)
    and before the user touches the tree UI — loading later would
    silently discard whatever tree(s) they'd already started building in
    this session.
    """
    envelope = _load_or_404(redis_conn, session_id)
    _require_finalized(envelope)

    try:
        project_trees = get_project_trees(engine, user_id, project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    session = envelope.session
    columns_result: dict[str, dict] = {}
    for column, tree in project_trees.items():
        if column not in session.categorized_columns:
            continue

        # "Known" = every entity the project's dictionary has ever
        # assigned a group to, for THIS column specifically. Anything else
        # in the newly-uploaded file is genuinely new to this project and
        # needs a human to sort it — core.tree's own detect_new_entities()
        # is the single source of truth for that split, not a hand-rolled
        # set difference here.
        previously_known = set(tree.assignment.keys())
        current_entities = sorted({v for v in session.df[column].to_list() if v is not None})
        new_entities = tree.detect_new_entities(current_entities, previously_known)

        session.trees[column] = tree
        # Only the project's OWN dictionary counts as "known" here — NOT
        # the full current_entities list. Entities that are new to the
        # project must keep is_new=True (see core.session.Session.
        # unique_entities) until the user actually assigns them, which is
        # what shows the "новое" badge in the UI and is exactly the signal
        # this endpoint's response surfaces below.
        session._previously_known_entities[column] = previously_known

        columns_result[column] = {
            "known_entities_count": len(current_entities) - len(new_entities),
            "new_entities": new_entities,
            "new_entities_count": len(new_entities),
        }

    save_session(redis_conn, session_id, session, envelope.finalized, envelope.header_row_index)

    return {"project_id": project_id, "columns": columns_result}


# ---------------------------------------------------------------------
# Инструменты (Unpivot, Compare) — see README "Архитектура интерфейса":
# one-off, stateless, anonymous utilities, deliberately NOT part of the
# Project pipeline above: no auth, nothing persisted, no group tree, no
# finalized/header_row_index envelope. The parsed table between preview and
# commit lives in Redis with a short TTL only (see backend/tools_store.py)
# — reusing the same connection as session_store.py because the deploy runs
# multiple uvicorn workers, NOT because this is a "session" in that
# module's sense.
# ---------------------------------------------------------------------


@app.post("/api/tools/preview")
async def tools_preview(file: UploadFile = File(...), store: ToolFileStore = Depends(get_tools_store)):
    """Shared first step for both Инструменты: parse the upload header-
    agnostically (exact same core.parsing.parse_file_raw() the main
    pipeline's /api/upload uses) and hand back a preview grid plus a
    short-lived token identifying the parsed table (see
    backend/tools_store.py — Redis-backed, short TTL, no session_id)."""
    suffix = Path(file.filename or "").suffix.lower()
    contents = await file.read()

    tmp_path: str
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        parsed = parse_file_raw(tmp_path)
    except UnsupportedFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        os.unlink(tmp_path)

    token = store.put(parsed.df)
    preview_rows = [list(row) for row in parsed.df.head(PREVIEW_ROW_LIMIT).rows()]

    return {
        "token": token,
        "filename": file.filename,
        "columns_count": parsed.df.width,
        "row_count": parsed.df.height,
        "preview_rows": preview_rows,
        "detected_encoding": parsed.detected_encoding,
        "detected_delimiter": parsed.detected_delimiter,
    }


def _load_tool_df(store: ToolFileStore, token: str):
    try:
        return store.get(token)
    except ToolTokenNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/tools/unpivot")
def tools_unpivot(body: UnpivotRequest, store: ToolFileStore = Depends(get_tools_store)):
    raw_df = _load_tool_df(store, body.token)

    try:
        data_df, column_names = promote_header_row(raw_df, body.header_row_index)
        id_names, value_names = resolve_unpivot_columns(column_names, body.id_columns, body.value_columns)
        long_df = unpivot_table(data_df, id_names, value_names)
    except (ColumnMappingError, UnpivotError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    export_single_sheet(tmp.name, long_df, sheet_name="Развёрнуто")

    return FileResponse(
        tmp.name,
        filename="gruper_unpivot.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        background=BackgroundTask(os.unlink, tmp.name),
    )


def _run_compare(body: CompareRequest, store: ToolFileStore):
    """Shared by /api/tools/compare (returns JSON) and .../compare/export
    (returns a file): both need the exact same two-file parse + match, so
    this is computed once here rather than duplicated. Recomputing per
    request (instead of caching the result behind a second token) is a
    deliberate simplicity choice — see README-equivalent commit message:
    Compare's input files are already small enough (same class as the main
    pipeline's Excel/CSV) that redoing the match on export is cheap, and it
    avoids a second kind of stored state for a one-shot tool."""
    raw_a = _load_tool_df(store, body.token_a)
    raw_b = _load_tool_df(store, body.token_b)

    try:
        data_a, entity_col_a, metric_col_a = finalize_columns(
            raw_a, body.header_row_index_a, body.entity_column_a, body.metric_column_a
        )
        data_b, entity_col_b, metric_col_b = finalize_columns(
            raw_b, body.header_row_index_b, body.entity_column_b, body.metric_column_b
        )
    except ColumnMappingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    entities_a = data_a[entity_col_a].to_list()
    values_a = [Decimal(str(v)) for v in data_a[metric_col_a].to_list()]
    entities_b = data_b[entity_col_b].to_list()
    values_b = [Decimal(str(v)) for v in data_b[metric_col_b].to_list()]

    return compare_entities(entities_a, values_a, entities_b, values_b)


@app.post("/api/tools/compare")
def tools_compare(body: CompareRequest, store: ToolFileStore = Depends(get_tools_store)):
    rows = _run_compare(body, store)

    def row_json(r):
        return {
            "entity": r.entity,
            "sum_a": _decimal_str(r.sum_a) if r.sum_a is not None else None,
            "sum_b": _decimal_str(r.sum_b) if r.sum_b is not None else None,
            "diff": _decimal_str(r.diff) if r.diff is not None else None,
            "status": r.status,
        }

    total_a = sum((r.sum_a for r in rows if r.sum_a is not None), Decimal(0))
    total_b = sum((r.sum_b for r in rows if r.sum_b is not None), Decimal(0))

    return {
        "rows": [row_json(r) for r in rows],
        "summary": {
            "matched": sum(1 for r in rows if r.status == "совпадает"),
            "mismatched": sum(1 for r in rows if r.status == "расхождение"),
            "only_a": sum(1 for r in rows if r.status == "только в A"),
            "only_b": sum(1 for r in rows if r.status == "только в B"),
            "total_a": _decimal_str(total_a),
            "total_b": _decimal_str(total_b),
        },
    }


@app.post("/api/tools/compare/export")
def tools_compare_export(body: CompareRequest, store: ToolFileStore = Depends(get_tools_store)):
    rows = _run_compare(body, store)

    result_df = pl.DataFrame(
        {
            "Сущность": [r.entity for r in rows],
            "Сумма A": [_decimal_str(r.sum_a) if r.sum_a is not None else "" for r in rows],
            "Сумма B": [_decimal_str(r.sum_b) if r.sum_b is not None else "" for r in rows],
            "Разница": [_decimal_str(r.diff) if r.diff is not None else "" for r in rows],
            "Статус": [r.status for r in rows],
        }
    )

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    export_single_sheet(tmp.name, result_df, sheet_name="Расхождения")

    return FileResponse(
        tmp.name,
        filename="gruper_compare.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        background=BackgroundTask(os.unlink, tmp.name),
    )


# ---------------------------------------------------------------------
# Инструменты (Mail Merge, Split by marker, Watermark) — Фаза 4. Same
# "Инструменты" contract as Unpivot/Compare above: no auth, nothing
# persisted, anonymous. Mail Merge reuses the existing
# preview(token)->commit flow (it needs a header-row-pick step, same as
# Unpivot/Compare, since placeholders are matched against real column
# names). Split and Watermark do NOT need that intermediate step — there's
# no column/row picking screen for either, just one file + one text field
# — so they're single-request endpoints (multipart upload straight to the
# result) rather than routed through ToolFileStore. That also sidesteps
# the whole "does this token survive across the 2 uvicorn workers" concern
# tools_store.py's docstring describes: with no state held between
# requests, there's nothing that CAN go to the wrong worker.
# ---------------------------------------------------------------------

_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _safe_filename(raw: str, fallback: str) -> str:
    cleaned = _UNSAFE_FILENAME_CHARS.sub("", raw).strip()
    cleaned = cleaned[:80]
    return cleaned or fallback


@app.post("/api/tools/mailmerge")
def tools_mailmerge(body: MailMergeRequest, store: ToolFileStore = Depends(get_tools_store)):
    """Excel (one row per recipient) + a {{Поле}} text template -> a .zip
    of N personalized PDFs, one per row. See core/mailmerge.py for the
    substitution/rendering logic; this endpoint is only the same thin
    HTTP wrapping every other route in this file already does."""
    raw_df = _load_tool_df(store, body.token)

    try:
        data_df, column_names = promote_header_row(raw_df, body.header_row_index)
    except ColumnMappingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        validate_template(body.template, column_names)
    except MailMergeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if data_df.height == 0:
        raise HTTPException(status_code=400, detail="В файле нет строк с данными после выбранной строки заголовка")

    rows = [list(r) for r in data_df.rows()]
    row_dicts = build_row_dicts(column_names, rows)
    first_column = column_names[0]

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    used_names: dict[str, int] = {}
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, row in enumerate(row_dicts, start=1):
            text = render_text(body.template, row)
            pdf_bytes = render_mailmerge_pdf(text)

            base_name = _safe_filename(row.get(first_column, ""), f"документ_{i}")
            name = base_name
            if name in used_names:
                used_names[name] += 1
                name = f"{name} ({used_names[name]})"
            else:
                used_names[name] = 0
            zf.writestr(f"{name}.pdf", pdf_bytes)

    return FileResponse(
        tmp.name,
        filename="gruper_mailmerge.zip",
        media_type="application/zip",
        background=BackgroundTask(os.unlink, tmp.name),
    )


@app.post("/api/tools/split")
async def tools_split(file: UploadFile = File(...), marker: str = Form(...)):
    """One PDF + one text marker -> a .zip of the PDF split into parts at
    every page the marker appears on. See core/pdf_split.py."""
    contents = await file.read()

    try:
        parts = split_by_marker(contents, marker)
    except SplitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for part in parts:
            zf.writestr(f"{part.name}.pdf", part.pdf_bytes)

    return FileResponse(
        tmp.name,
        filename="gruper_split.zip",
        media_type="application/zip",
        background=BackgroundTask(os.unlink, tmp.name),
    )


@app.post("/api/tools/watermark")
async def tools_watermark(file: UploadFile = File(...), text: str = Form(...)):
    """One PDF + watermark text -> the same PDF back with the text stamped
    diagonally on every page. See core/watermark.py."""
    contents = await file.read()

    try:
        result_bytes = add_watermark(contents, text)
    except WatermarkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(result_bytes)
    tmp.close()

    return FileResponse(
        tmp.name,
        filename="gruper_watermark.pdf",
        media_type="application/pdf",
        background=BackgroundTask(os.unlink, tmp.name),
    )
