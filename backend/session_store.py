"""Redis-backed persistence for core.session.Session across HTTP requests.

core.session.Session holds a Polars DataFrame plus an in-memory TreeStore —
neither survives past a single Python process, and HTTP is stateless, so
the API layer needs a way to round-trip a Session through Redis between
requests. Each session is stored as a single Redis hash with two fields:

- "df": the DataFrame as Arrow IPC bytes (pl.DataFrame.write_ipc /
  pl.read_ipc via a BytesIO buffer) — near-zero (de)serialization cost,
  the standard way to move a Polars DataFrame through a byte store.
- "meta": everything else, as JSON — whether columns have been finalized
  yet, the chosen header row index, column names, the group tree, the
  entity->Split assignment map, and which entities have ever been seen.
  Split.fraction is a Decimal (JSON has no Decimal type), so it round-trips
  as a string and gets parsed back through Decimal(str(...)) on load —
  float never touches money anywhere in this path.

Sessions are anonymous and ephemeral by design (no auth in this phase —
see project brief): every save refreshes a TTL, so an abandoned upload
ages out of Redis on its own instead of accumulating forever, while an
actively-worked session never expires mid-use.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO

import polars as pl
from redis import Redis

from core.reconcile import Group, Split
from core.session import Session
from core.tree import TreeStore

SESSION_KEY_PREFIX = "gruper:session:"
SESSION_TTL_SECONDS = 24 * 60 * 60


class SessionNotFoundError(Exception):
    """session_id doesn't exist in Redis — never created, or expired past
    its TTL. The API layer maps this to an HTTP 404."""


@dataclass
class SessionEnvelope:
    """Everything the API needs about a session besides the core.Session
    object itself: whether column mapping has been finalized yet, and
    which raw row the user picked as the header (kept only for reference /
    re-display — core.Session itself has no concept of "header row")."""

    session: Session
    finalized: bool
    header_row_index: int | None


def _redis_key(session_id: str) -> str:
    return f"{SESSION_KEY_PREFIX}{session_id}"


def get_redis_client() -> Redis:
    """Builds the Redis connection the HTTP layer uses. Reuses the same
    "connection is a parameter, not a hardcoded global" convention
    core/queues.py already established. Overridden in tests via FastAPI's
    dependency_overrides with a fakeredis client, so the test suite needs
    no live Redis server."""
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return Redis.from_url(url)


def _group_to_dict(g: Group) -> dict:
    return {"id": g.id, "parent_id": g.parent_id, "name": g.name}


def _group_from_dict(d: dict) -> Group:
    return Group(id=d["id"], parent_id=d["parent_id"], name=d["name"])


def _split_to_dict(s: Split) -> dict:
    return {"group_id": s.group_id, "fraction": str(s.fraction)}


def _split_from_dict(d: dict) -> Split:
    return Split(group_id=d["group_id"], fraction=Decimal(d["fraction"]))


def save_session(
    redis_conn: Redis,
    session_id: str,
    session: Session,
    finalized: bool,
    header_row_index: int | None,
) -> None:
    buffer = BytesIO()
    session.df.write_ipc(buffer)

    meta = {
        "finalized": finalized,
        "header_row_index": header_row_index,
        "entity_column": session.entity_column,
        "metric_column": session.metric_column,
        "groups": [_group_to_dict(g) for g in session.tree.groups.values()],
        "assignment": {
            entity: [_split_to_dict(s) for s in splits]
            for entity, splits in session.tree.assignment.items()
        },
        "previously_known_entities": sorted(session._previously_known_entities),
    }

    key = _redis_key(session_id)
    redis_conn.hset(key, mapping={"df": buffer.getvalue(), "meta": json.dumps(meta)})
    redis_conn.expire(key, SESSION_TTL_SECONDS)


def load_session(redis_conn: Redis, session_id: str) -> SessionEnvelope:
    key = _redis_key(session_id)
    raw = redis_conn.hgetall(key)
    if not raw:
        raise SessionNotFoundError(f"Session {session_id!r} not found or expired")

    # redis-py returns bytes keys/values by default (no decode_responses
    # set on the client), so index with byte-string keys here.
    df_bytes = raw[b"df"]
    meta = json.loads(raw[b"meta"])

    df = pl.read_ipc(BytesIO(df_bytes))

    tree = TreeStore()
    tree.groups = {g["id"]: _group_from_dict(g) for g in meta["groups"]}
    tree.assignment = {
        entity: [_split_from_dict(s) for s in splits] for entity, splits in meta["assignment"].items()
    }

    session = Session(
        df=df,
        entity_column=meta["entity_column"],
        metric_column=meta["metric_column"],
        tree=tree,
    )
    session._previously_known_entities = set(meta["previously_known_entities"])

    return SessionEnvelope(
        session=session,
        finalized=meta["finalized"],
        header_row_index=meta["header_row_index"],
    )


def delete_session(redis_conn: Redis, session_id: str) -> None:
    redis_conn.delete(_redis_key(session_id))
