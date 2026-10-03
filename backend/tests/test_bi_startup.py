"""Restart is a storage operation, never an implicit model invocation."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from insight.runtime import Runtime


@pytest.mark.asyncio
async def test_startup_does_not_seed_or_call_models(monkeypatch):
    import insight.runtime as module

    class Store:
        async def setup(self):
            pass

        @classmethod
        @asynccontextmanager
        async def from_conn_string(cls, value):
            yield cls()

    class Repo:
        def __init__(self, value):
            pass

        def setup(self):
            pass

        def recover_interrupted(self):
            return 0

    rt = Runtime.__new__(Runtime)
    from contextlib import AsyncExitStack
    rt.stack = AsyncExitStack()
    rt.settings = SimpleNamespace(scenario_root="unused", data_dir="unused", postgres_uri=SimpleNamespace(get_secret_value=lambda: "test-dsn"), embedding_base_url="configured-but-not-called")
    rt.scenarios = {"demo": {"id": "demo"}}
    rt.bind = lambda saver, store: None

    async def seed(*args):
        pytest.fail("startup called seed_template, which can invoke embedding")

    rt.seed_template = seed
    monkeypatch.setattr(module, "generate_all", lambda *args: {})
    monkeypatch.setattr(module, "Repository", Repo)
    monkeypatch.setattr(module, "AsyncPostgresSaver", Store)
    monkeypatch.setattr(module, "AsyncPostgresStore", Store)
    monkeypatch.setattr(module, "Embedder", lambda *args: object())
    monkeypatch.setattr(module, "MemoryService", lambda *args: object())
    await rt.start()
    assert isinstance(rt.repository, Repo)
    await rt.stack.aclose()
