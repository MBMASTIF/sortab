"""PostgreSQL-backed persistence for the CRUD layer around "Проекты".

Deliberately separate from backend/session_store.py — see the README's
"Проекты" vs "сессии" split: a session (Redis, 24h TTL) is one parsed file
in progress; a Project (Postgres, permanent) is a saved category tree +
entity dictionary for a REPEATED report type, reused across many future
files. This module only ever stores the tree/dictionary — never a raw
DataFrame — so it's intentionally lightweight compared to session_store.py.

Uses SQLAlchemy Core (no ORM layer — the project's existing modules are
all plain functions over plain data, and Core keeps that style) with a
database-agnostic schema:
- `tree_json` uses sa.JSON with a PostgreSQL JSONB variant, so production
  gets real JSONB (as specified in the README) while the test suite can
  run the exact same code against an in-memory SQLite engine (no live
  Postgres needed to run `pytest`, mirroring how backend/session_store.py
  tests run against fakeredis instead of a live Redis).
- ids are uuid4 strings (sa.String), not a native UUID column type — same
  pragmatic choice backend/main.py already makes for session_id, and it
  keeps the schema identical across SQLite (tests) and Postgres (prod).
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()

# JSON on every backend, upgraded to real JSONB specifically on PostgreSQL.
_JSON_VARIANT = sa.JSON().with_variant(JSONB(), "postgresql")

users = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("email", sa.String(320), nullable=False, unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

projects = sa.Table(
    "projects",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
    sa.Column("name", sa.String(255), nullable=False),
    # entity_column is kept = categorized_columns[0] for back-compat display
    # (e.g. the project list card) now that a Project can hold more than
    # one independent category tree — see categorized_columns below for
    # the full list, in the same tab order the session used.
    sa.Column("entity_column", sa.String(255), nullable=False),
    sa.Column("metric_column", sa.String(255), nullable=False),
    sa.Column("categorized_columns", _JSON_VARIANT, nullable=False),
    # {column_name: {"groups": [...], "assignment": {...}}} — one
    # tree_serialization.tree_to_dict() blob per categorized column,
    # keyed by column name (was a single tree's dict before this phase).
    sa.Column("tree_json", _JSON_VARIANT, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

_engine: sa.engine.Engine | None = None


def get_engine() -> sa.engine.Engine:
    """FastAPI dependency (see backend/main.py get_db()). Overridden in
    tests with an in-memory SQLite engine, the same "swap the dependency,
    not the code" pattern backend/main.py's get_redis() already uses for
    Redis vs fakeredis."""
    global _engine
    if _engine is None:
        # DATABASE_URL must spell the driver explicitly as
        # "postgresql+psycopg2://..." in production — verified live on
        # the server (2026-09-27): SQLAlchemy 2.1's default postgresql
        # dialect resolution tried to import `psycopg` (v3, not
        # installed) when the URL just said "postgresql://", even though
        # psycopg2-binary was the driver actually installed. A bare
        # "postgresql://" is a silent 500 waiting to happen, not a safe
        # default.
        url = os.environ.get("DATABASE_URL", "sqlite:///./gruper_dev.db")
        _engine = sa.create_engine(url, future=True)
        metadata.create_all(_engine)
    return _engine
