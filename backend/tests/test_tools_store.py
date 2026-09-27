import polars as pl
import pytest

from backend import tools_store
from backend.tools_store import ToolFileStore, ToolTokenNotFoundError


def test_put_then_get_returns_the_same_dataframe():
    store = ToolFileStore(ttl_seconds=60)
    df = pl.DataFrame({"a": [1, 2, 3]})
    token = store.put(df)
    assert store.get(token).to_dicts() == df.to_dicts()


def test_unknown_token_raises_not_found():
    store = ToolFileStore(ttl_seconds=60)
    with pytest.raises(ToolTokenNotFoundError):
        store.get("does-not-exist")


def test_expired_token_raises_not_found(monkeypatch):
    fake_clock = [1000.0]
    monkeypatch.setattr(tools_store.time, "monotonic", lambda: fake_clock[0])

    store = ToolFileStore(ttl_seconds=5)
    token = store.put(pl.DataFrame({"a": [1]}))

    fake_clock[0] += 10  # past the 5s TTL
    with pytest.raises(ToolTokenNotFoundError):
        store.get(token)


def test_expiry_is_purged_lazily_on_next_access(monkeypatch):
    """Not just that an expired token 404s — the entry is actually removed
    from memory on the next put/get, not left to accumulate forever."""
    fake_clock = [1000.0]
    monkeypatch.setattr(tools_store.time, "monotonic", lambda: fake_clock[0])

    store = ToolFileStore(ttl_seconds=5)
    store.put(pl.DataFrame({"a": [1]}))
    assert len(store._entries) == 1

    fake_clock[0] += 10
    store.put(pl.DataFrame({"a": [2]}))  # triggers a purge before inserting
    assert len(store._entries) == 1  # the expired one is gone, only the new one remains


def test_get_tool_store_returns_a_usable_shared_instance():
    store = tools_store.get_tool_store()
    token = store.put(pl.DataFrame({"a": [1]}))
    assert store.get(token) is not None
