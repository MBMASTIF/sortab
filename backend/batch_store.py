"""Ephemeral storage for a multi-source upload batch ("несколько
источников" / "объединение листов", каталог услуг пп. 2 и 5) between the
per-source preview+header-row step (POST /api/upload/batch) and the
consolidate step (POST /api/upload/batch/{id}/consolidate).

Same Redis-backed pattern as backend/tools_store.py, for the identical
reason documented there (not repeated in full here): the deployed service
runs 2 uvicorn workers, and these two requests have no guarantee of
landing on the same worker, so an in-process dict would 404
unpredictably.

A batch is NOT a Session (backend/session_store.py) — it has no tree, no
finalized flag, no single header_row_index of its own (each source has
its OWN header row — that's the whole reason this exists as a separate,
short-lived structure instead of trying to shoehorn several raw grids
into one core.session.Session). Once backend/consolidate.py's
consolidate_sources() succeeds, its result becomes a normal Session and
the batch is discarded.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from io import BytesIO

import polars as pl
from redis import Redis

BATCH_TTL_SECONDS = 30 * 60
BATCH_KEY_PREFIX = "gruper:upload-batch:"


class BatchNotFoundError(Exception):
    """batch_id doesn't exist in Redis — never created, already consumed
    by a successful consolidate call, or expired past its TTL. The API
    layer maps this to an HTTP 404."""


@dataclass
class BatchSource:
    """One raw (header-agnostic) source sitting in a batch, waiting for
    the user to pick its header row and label. filename doubles as the
    frontend's default label — for a sheet expanded out of a multi-sheet
    upload it already reads "{original filename} — {sheet name}" by the
    time it gets here (see backend/main.py::upload_batch)."""

    filename: str
    raw_df: pl.DataFrame


def _redis_key(batch_id: str) -> str:
    return f"{BATCH_KEY_PREFIX}{batch_id}"


class UploadBatchStore:
    """Thin wrapper around a Redis connection — a class (not bare module
    functions) so tests can hand it an isolated fakeredis client, same as
    backend/tools_store.py::ToolFileStore and every other Redis-backed
    store in this project."""

    def __init__(self, redis_conn: Redis, ttl_seconds: int = BATCH_TTL_SECONDS) -> None:
        self._redis = redis_conn
        self._ttl_seconds = ttl_seconds

    def create(self, sources: list[BatchSource]) -> str:
        batch_id = str(uuid.uuid4())
        key = _redis_key(batch_id)

        mapping: dict[str, bytes | str] = {
            "filenames": json.dumps([source.filename for source in sources]),
        }
        for i, source in enumerate(sources):
            buffer = BytesIO()
            source.raw_df.write_ipc(buffer)
            mapping[f"df_{i}"] = buffer.getvalue()

        self._redis.hset(key, mapping=mapping)
        self._redis.expire(key, self._ttl_seconds)
        return batch_id

    def get(self, batch_id: str) -> list[BatchSource]:
        key = _redis_key(batch_id)
        raw = self._redis.hgetall(key)
        if not raw:
            raise BatchNotFoundError(f"Batch {batch_id!r} not found or expired")

        # redis-py returns bytes keys/values by default — index with
        # byte-string keys here, same convention session_store.py uses.
        filenames = json.loads(raw[b"filenames"])
        sources: list[BatchSource] = []
        for i, filename in enumerate(filenames):
            df_bytes = raw[f"df_{i}".encode()]
            sources.append(BatchSource(filename=filename, raw_df=pl.read_ipc(BytesIO(df_bytes))))
        return sources

    def delete(self, batch_id: str) -> None:
        self._redis.delete(_redis_key(batch_id))
