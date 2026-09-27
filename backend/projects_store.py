"""CRUD for "Проекты" (see backend/db.py for the schema/why-Postgres, and
backend/tree_serialization.py for the shared TreeStore<->dict format).

Kept HTTP-free on purpose, same principle core/session.py already follows:
backend/main.py is a thin wrapper that turns these into endpoints and maps
the two domain errors below onto HTTP codes (409 / 404).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa

from backend.db import projects, users
from backend.tree_serialization import tree_from_dict, tree_to_dict
from core.tree import TreeStore

# The only monetization limit that's real right now (see README "Решено:
# тарифы..." — everything else needs actual billing, which doesn't exist
# yet). Hardcoded, not read from a settings table: there is no billing
# system to source it from, and the whole point of a phase-2 constant is
# that it's cheap to change in one place once real tiers exist.
FREE_TIER_PROJECT_LIMIT = 1


class ProjectLimitError(Exception):
    """Free-tier user already has FREE_TIER_PROJECT_LIMIT project(s)."""


class ProjectNotFoundError(Exception):
    """Unknown project id, or a project id that exists but belongs to a
    different user — both map to the same 404 from the caller's point of
    view, deliberately: a project's existence is not something another
    user's browser should be able to probe for."""


def get_or_create_user(engine: sa.engine.Engine, email: str) -> str:
    with engine.begin() as conn:
        row = conn.execute(sa.select(users.c.id).where(users.c.email == email)).first()
        if row is not None:
            return row.id
        user_id = str(uuid.uuid4())
        conn.execute(users.insert().values(id=user_id, email=email, created_at=datetime.now(timezone.utc)))
        return user_id


def count_projects(engine: sa.engine.Engine, user_id: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            sa.select(sa.func.count()).select_from(projects).where(projects.c.user_id == user_id)
        ).scalar_one()


def create_project(
    engine: sa.engine.Engine,
    user_id: str,
    name: str,
    entity_column: str,
    metric_column: str,
    tree: TreeStore,
) -> dict:
    if count_projects(engine, user_id) >= FREE_TIER_PROJECT_LIMIT:
        raise ProjectLimitError(
            f"На бесплатном тарифе можно сохранить только {FREE_TIER_PROJECT_LIMIT} проект — "
            "удалите текущий или перейдите на тариф «Профи» для безлимита"
        )
    project_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            projects.insert().values(
                id=project_id,
                user_id=user_id,
                name=name,
                entity_column=entity_column,
                metric_column=metric_column,
                tree_json=tree_to_dict(tree),
                created_at=now,
                updated_at=now,
            )
        )
    return _row_to_summary(_fetch_row(engine, project_id))


def list_projects(engine: sa.engine.Engine, user_id: str) -> list[dict]:
    with engine.begin() as conn:
        rows = (
            conn.execute(
                sa.select(projects)
                .where(projects.c.user_id == user_id)
                .order_by(projects.c.updated_at.desc())
            )
            .mappings()
            .all()
        )
    return [_row_to_summary(r) for r in rows]


def get_project(engine: sa.engine.Engine, user_id: str, project_id: str) -> dict:
    row = _fetch_row(engine, project_id)
    if row is None or row["user_id"] != user_id:
        raise ProjectNotFoundError(f"Project {project_id!r} not found")
    summary = _row_to_summary(row)
    summary["tree"] = row["tree_json"]
    return summary


def get_project_tree(engine: sa.engine.Engine, user_id: str, project_id: str) -> TreeStore:
    row = _fetch_row(engine, project_id)
    if row is None or row["user_id"] != user_id:
        raise ProjectNotFoundError(f"Project {project_id!r} not found")
    return tree_from_dict(row["tree_json"])


def _fetch_row(engine: sa.engine.Engine, project_id: str):
    with engine.begin() as conn:
        return conn.execute(sa.select(projects).where(projects.c.id == project_id)).mappings().first()


def _row_to_summary(row) -> dict:
    groups_count = len(row["tree_json"].get("groups", []))
    return {
        "id": row["id"],
        "name": row["name"],
        "entity_column": row["entity_column"],
        "metric_column": row["metric_column"],
        "groups_count": groups_count,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }
