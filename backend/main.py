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
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
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
from backend.columns import ColumnMappingError, finalize_columns
from backend.db import get_engine
from backend.projects_store import (
    ProjectLimitError,
    ProjectNotFoundError,
    create_project,
    get_or_create_user,
    get_project,
    get_project_tree,
    list_projects,
)
from backend.session_store import (
    SessionEnvelope,
    SessionNotFoundError,
    get_redis_client,
    load_session,
    save_session,
)
from core.export import build_detail_sheet, build_summary_sheet, export_workbook
from core.parsing import UnsupportedFileError, parse_file_raw
from core.session import Session, SessionError
from core.tree import GroupNotEmptyError, NotALeafError, TreeError

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
    entity_column: int
    metric_column: int


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


def _decimal_str(value: Decimal) -> str:
    return str(value)


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

    try:
        parsed = parse_file_raw(tmp_path)
    except UnsupportedFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
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


@app.post("/api/session/{session_id}/columns")
def set_columns(session_id: str, body: ColumnsRequest, redis_conn: Redis = Depends(get_redis)):
    envelope = _load_or_404(redis_conn, session_id)
    raw_df = envelope.session.df

    try:
        data_df, entity_col_name, metric_col_name = finalize_columns(
            raw_df, body.header_row_index, body.entity_column, body.metric_column
        )
    except ColumnMappingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session = Session(df=data_df, tree=envelope.session.tree)
    try:
        session.set_columns(entity_col_name, metric_col_name)
    except SessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session._previously_known_entities = envelope.session._previously_known_entities

    save_session(redis_conn, session_id, session, finalized=True, header_row_index=body.header_row_index)

    return {
        "entity_column": entity_col_name,
        "metric_column": metric_col_name,
        "row_count": data_df.height,
    }


@app.get("/api/session/{session_id}/entities")
def get_entities(session_id: str, redis_conn: Redis = Depends(get_redis)):
    envelope = _load_or_404(redis_conn, session_id)
    _require_finalized(envelope)

    return [
        {"value": e.value, "occurrences": e.occurrences, "is_new": e.is_new}
        for e in envelope.session.unique_entities()
    ]


@app.post("/api/session/{session_id}/groups", status_code=201)
def create_group(session_id: str, body: CreateGroupRequest, redis_conn: Redis = Depends(get_redis)):
    envelope = _load_or_404(redis_conn, session_id)

    try:
        group = envelope.session.create_group(body.group_id, body.name, body.parent_id)
    except TreeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    save_session(redis_conn, session_id, envelope.session, envelope.finalized, envelope.header_row_index)

    return {"id": group.id, "name": group.name, "parent_id": group.parent_id}


@app.get("/api/session/{session_id}/groups")
def get_groups(session_id: str, redis_conn: Redis = Depends(get_redis)):
    """Lets a client re-derive the group tree after a hard page reload —
    the tree lives in Redis via session.tree, it was just never exposed
    over HTTP before. See design-prototype/app.html's init(), which used
    to rebuild the tree from nothing but its own in-memory JS state and
    lost it on refresh."""
    envelope = _load_or_404(redis_conn, session_id)
    return [
        {"id": g.id, "parent_id": g.parent_id, "name": g.name} for g in envelope.session.tree.as_group_list()
    ]


@app.delete("/api/session/{session_id}/groups/{group_id}", status_code=204)
def delete_group(session_id: str, group_id: str, redis_conn: Redis = Depends(get_redis)):
    envelope = _load_or_404(redis_conn, session_id)

    try:
        envelope.session.tree.remove_group(group_id)
    except GroupNotEmptyError as exc:
        # Mirrors the prototype's alert('В этой категории есть значения —
        # сначала перенесите их') — a 409, not a bare 500, because this is
        # an expected, recoverable user action, not a server fault.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TreeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    save_session(redis_conn, session_id, envelope.session, envelope.finalized, envelope.header_row_index)


@app.post("/api/session/{session_id}/assign")
def assign_entities(session_id: str, body: AssignRequest, redis_conn: Redis = Depends(get_redis)):
    envelope = _load_or_404(redis_conn, session_id)
    _require_finalized(envelope)

    try:
        envelope.session.assign(body.entity_ids, body.group_id)
    except NotALeafError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TreeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    save_session(redis_conn, session_id, envelope.session, envelope.finalized, envelope.header_row_index)

    return {"assigned": len(body.entity_ids), "group_id": body.group_id}


@app.get("/api/session/{session_id}/summary")
def get_summary(session_id: str, redis_conn: Redis = Depends(get_redis)):
    envelope = _load_or_404(redis_conn, session_id)
    _require_finalized(envelope)

    result = envelope.session.current_summary()
    groups = envelope.session.tree.as_group_list()

    return {
        "direct_totals": {k: _decimal_str(v) for k, v in result.direct_totals.items()},
        "rollup_totals": {k: _decimal_str(v) for k, v in result.rollup_totals.items()},
        "unassigned_total": _decimal_str(result.unassigned_total),
        "unassigned_entities": sorted({r.entity for r in result.unassigned_rows}),
        "grand_total": _decimal_str(result.grand_total(groups)),
    }


@app.get("/api/session/{session_id}/export")
def export_session(session_id: str, redis_conn: Redis = Depends(get_redis)):
    envelope = _load_or_404(redis_conn, session_id)
    _require_finalized(envelope)

    session = envelope.session
    groups = session.tree.as_group_list()
    result = session.current_summary()

    detail_df = build_detail_sheet(session.df, session.entity_column, session.tree.assignment, groups)
    summary_df = build_summary_sheet(result, groups)

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    export_workbook(tmp.name, detail_df, summary_df)

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
            session.entity_column,
            session.metric_column,
            session.tree,
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
    """The actual point of the whole Projects feature: pull a saved
    tree+dictionary into a freshly-uploaded working session, BEFORE the
    user starts sorting anything, so only genuinely new entities need
    manual attention this time around.

    Must run after /columns (the session needs entity_column resolved to
    know the current file's unique entity values) and before the user
    touches the tree UI — loading later would silently discard whatever
    tree they'd already started building in this session.
    """
    envelope = _load_or_404(redis_conn, session_id)
    _require_finalized(envelope)

    try:
        tree = get_project_tree(engine, user_id, project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    session = envelope.session
    # "Known" = every entity the project's dictionary has ever assigned a
    # group to. Anything else in the newly-uploaded file is genuinely new
    # to this project and needs a human to sort it — core.tree's own
    # detect_new_entities() is the single source of truth for that split,
    # not a hand-rolled set difference here.
    previously_known = set(tree.assignment.keys())
    current_entities = sorted({v for v in session.df[session.entity_column].to_list() if v is not None})
    new_entities = tree.detect_new_entities(current_entities, previously_known)

    session.tree = tree
    # Only the project's OWN dictionary counts as "known" here — NOT the
    # full current_entities list. Entities that are new to the project
    # must keep is_new=True (see core.session.Session.unique_entities)
    # until the user actually assigns them, which is what shows the
    # "новое" badge in the UI and is exactly the signal this endpoint's
    # response surfaces below.
    session._previously_known_entities = previously_known

    save_session(redis_conn, session_id, session, envelope.finalized, envelope.header_row_index)

    return {
        "project_id": project_id,
        "known_entities_count": len(current_entities) - len(new_entities),
        "new_entities": new_entities,
        "new_entities_count": len(new_entities),
    }
