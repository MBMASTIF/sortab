"""Ephemeral, in-process storage for the raw parsed DataFrame behind the
two "Инструменты" (Unpivot, Compare) preview->commit flows.

Architecture decision (see task brief — "реши сам, задокументируй выбор"):
these tools are explicitly NOT part of the Project pipeline (README
"Архитектура интерфейса" — Инструменты are one-off, stateless, anonymous,
nothing saved). backend/session_store.py's Redis-backed Session exists to
survive the *multi-step, potentially long-lived* group-tree-building
workflow (upload -> columns -> assign many times -> export, over minutes
or longer, possibly resumed after a reload). Neither Инструмент needs that:
Unpivot is upload -> pick columns -> download in one short sitting, and
Compare is upload A -> upload B -> compare -> download, also one sitting.
Putting the parsed table in a Redis "session" would mean either quietly
building session-shaped state for something the task brief and README both
say is explicitly not a session, or plumbing a whole second
finalized/header_row_index envelope through Redis for no behavioral gain.

So: a plain in-memory dict, keyed by a one-time uuid4 token, holding the
already-parsed Polars DataFrame directly (no serialization cost, unlike
session_store's Arrow-IPC round trip — it never needs to leave this
process). TTL is short (see TOKEN_TTL_SECONDS) — long enough to comfortably
cover "upload, look at the preview, pick columns, submit" without feeling
rushed, short enough that an abandoned upload doesn't sit in memory.

Known, accepted limitation (documented, not discovered later): this only
works within a single process. If backend/main.py is ever run with
multiple uvicorn/gunicorn workers, a token minted by worker A is invisible
to worker B. Today's deploy (see project CLAUDE.md-equivalent instructions)
runs one `uvicorn` process under systemd, so this is fine as built; if the
service is ever scaled to multiple workers, this store — and only this
store, nothing else in the tools pipeline — would need to move to Redis
with a short TTL, mirroring session_store's pattern.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

import polars as pl

TOKEN_TTL_SECONDS = 15 * 60


class ToolTokenNotFoundError(Exception):
    """token doesn't exist (never minted, already expired, or the process
    restarted). The API layer maps this to an HTTP 404."""


@dataclass
class _Entry:
    df: pl.DataFrame
    expires_at: float


class ToolFileStore:
    """Not a module-level global by accident of naming — an actual class,
    so tests can construct an isolated instance instead of mutating shared
    process state (see backend/tests/test_tools_store.py)."""

    def __init__(self, ttl_seconds: float = TOKEN_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def put(self, df: pl.DataFrame) -> str:
        token = str(uuid.uuid4())
        with self._lock:
            self._purge_expired_locked()
            self._entries[token] = _Entry(df=df, expires_at=time.monotonic() + self._ttl_seconds)
        return token

    def get(self, token: str) -> pl.DataFrame:
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(token)
            if entry is None:
                raise ToolTokenNotFoundError(
                    f"Token {token!r} not found or expired — please re-upload the file"
                )
            return entry.df

    def _purge_expired_locked(self) -> None:
        now = time.monotonic()
        expired = [key for key, entry in self._entries.items() if entry.expires_at < now]
        for key in expired:
            del self._entries[key]


_default_store = ToolFileStore()


def get_tool_store() -> ToolFileStore:
    """FastAPI dependency accessor — mirrors backend/session_store.py's
    get_redis_client() shape so main.py's endpoints read the same way as
    every other dependency in this file."""
    return _default_store
