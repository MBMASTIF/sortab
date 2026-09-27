import fakeredis
import polars as pl
import pytest

from backend.tools_store import ToolFileStore, ToolTokenNotFoundError


@pytest.fixture
def redis_conn():
    return fakeredis.FakeStrictRedis()


def test_put_then_get_returns_the_same_dataframe(redis_conn):
    store = ToolFileStore(redis_conn, ttl_seconds=60)
    df = pl.DataFrame({"a": [1, 2, 3]})
    token = store.put(df)
    assert store.get(token).to_dicts() == df.to_dicts()


def test_unknown_token_raises_not_found(redis_conn):
    store = ToolFileStore(redis_conn, ttl_seconds=60)
    with pytest.raises(ToolTokenNotFoundError):
        store.get("does-not-exist")


def test_expired_token_raises_not_found(redis_conn):
    """Simulates TTL expiry by deleting the key directly rather than
    sleeping past a real TTL (slow, flaky) or relying on fakeredis's
    sub-second expiry precision (fakeredis's own EXPIRE/PEXPIRE timing
    isn't reliable at 0ms, confirmed by running this against a real TTL
    and seeing it not fire) — from ToolFileStore.get()'s point of view, an
    expired key and a deleted key are indistinguishable, so this exercises
    the exact same code path a real expiry would hit."""
    store = ToolFileStore(redis_conn, ttl_seconds=1)
    token = store.put(pl.DataFrame({"a": [1]}))

    redis_conn.delete(f"gruper:tool-token:{token}")

    with pytest.raises(ToolTokenNotFoundError):
        store.get(token)


def test_two_store_instances_share_state_via_the_same_redis_connection(redis_conn):
    """The whole point of the Redis-backed rewrite: a token minted through
    one ToolFileStore instance (standing in for "worker 1") must be
    readable through a second instance built on the same Redis connection
    ("worker 2") — this is exactly the multi-worker scenario that broke
    the original in-process dict version."""
    store_worker_1 = ToolFileStore(redis_conn)
    store_worker_2 = ToolFileStore(redis_conn)

    token = store_worker_1.put(pl.DataFrame({"a": [42]}))
    result = store_worker_2.get(token)
    assert result.to_dicts() == [{"a": 42}]
