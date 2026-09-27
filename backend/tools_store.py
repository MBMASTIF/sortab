"""Ephemeral storage for the raw parsed DataFrame behind the two
"Инструменты" (Unpivot, Compare) preview->commit flows.

Course correction, measured not guessed: the first version of this module
was a plain in-process dict, reasoned as "these tools aren't a Project
session, so they don't need Redis" (see README "Архитектура интерфейса" —
Инструменты are one-off/anonymous/stateless, unlike backend/session_store's
Project-pipeline Session). That reasoning about the *data shape* was right,
but it implicitly assumed a single backend process. Checking the actual
deployed systemd unit (`systemctl cat gruper-backend`) before shipping
this showed `ExecStart=... uvicorn backend.main:app ... --workers 2` — two
independent OS processes behind the same port, with the OS load-balancing
connections between them. A token minted by worker 1's in-process dict is
simply invisible to worker 2 — the preview and commit requests for the
same tool run have no guarantee of landing on the same worker, so an
in-process store would 404 unpredictably, roughly half the time. That's
not an edge case to document and accept; it would make the feature flaky
in production. So this now stores the raw table's bytes in Redis instead,
reusing the exact same Redis connection (and the same `get_redis`
dependency/fakeredis-override pattern) session_store.py already established
for the identical reason.

This is still NOT a "session" in the Project-pipeline sense — no
TreeStore, no finalized flag, no header_row_index envelope, just
`token -> Arrow IPC bytes` with a short TTL, exactly the same shape the
in-process version had, only relocated to somewhere every worker can see
it. That's the whole fix.
"""

from __future__ import annotations

import uuid
from io import BytesIO

import polars as pl
from redis import Redis

TOKEN_TTL_SECONDS = 15 * 60
TOOL_TOKEN_KEY_PREFIX = "gruper:tool-token:"


class ToolTokenNotFoundError(Exception):
    """token doesn't exist (never minted, already expired, or minted
    against a different Redis than the one now being read). The API layer
    maps this to an HTTP 404."""


def _redis_key(token: str) -> str:
    return f"{TOOL_TOKEN_KEY_PREFIX}{token}"


class ToolFileStore:
    """Thin wrapper around a Redis connection — a class (not bare module
    functions) so tests can hand it an isolated fakeredis client instead of
    mutating shared state (see backend/tests/test_tools_api.py, which
    overrides the underlying get_redis dependency exactly like
    test_api.py already does for the Project pipeline)."""

    def __init__(self, redis_conn: Redis, ttl_seconds: int = TOKEN_TTL_SECONDS) -> None:
        self._redis = redis_conn
        self._ttl_seconds = ttl_seconds

    def put(self, df: pl.DataFrame) -> str:
        token = str(uuid.uuid4())
        buffer = BytesIO()
        df.write_ipc(buffer)
        self._redis.set(_redis_key(token), buffer.getvalue(), ex=self._ttl_seconds)
        return token

    def get(self, token: str) -> pl.DataFrame:
        raw = self._redis.get(_redis_key(token))
        if raw is None:
            raise ToolTokenNotFoundError(
                f"Token {token!r} not found or expired — please re-upload the file"
            )
        return pl.read_ipc(BytesIO(raw))
