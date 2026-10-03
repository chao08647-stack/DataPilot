import asyncio
from copy import deepcopy

import pytest
from langgraph.store.memory import InMemoryStore

from insight.memory import MemoryService, weighted_rrf
from insight.repository import InMemoryRepository


class TinyEmbedder:
    model = "bge-m3-test"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private endpoint must not leak")
        return [[1.0] + [0.0] * 1023 for _ in texts]


@pytest.fixture
def scenario():
    return {
        "id": "ecommerce", "schema_version": "1",
        "business_knowledge": [{"id": "returns", "title": "退款说明", "content": "refund 申请不是成功退款", "tags": ["refund"]}],
        "preferences": [{"id": "group", "title": "默认分组", "key": "group_by", "value": "channel", "description": "按渠道分组"}],
        "repair_experiences": [{
            "id": "duplicate", "title": "重复计数 refund join", "content": "先聚合再关联，避免重复计数 refund join", "error_category": "semantic", "tables": ["orders"],
            "fixture_sql": ["CREATE TABLE orders(amount INT)", "INSERT INTO orders VALUES (10),(20)"],
            "broken_sql": "SELECT COUNT(*) FROM orders", "fixed_sql": "SELECT SUM(amount) FROM orders", "expected": [[30]],
        }],
    }


def service(embedder=None, store=None):
    return MemoryService(store or InMemoryStore(), InMemoryRepository(), embedder)


@pytest.mark.parametrize("expiry", ["2000-01-01T00:00:00Z", "not-a-date"])
async def test_expired_canonical_records_and_preferences_are_never_loaded(scenario, expiry):
    memory = service(TinyEmbedder())
    await memory.seed(scenario)
    await memory.update("ecommerce", "preference:group", active=True)
    for record in await memory.list("ecommerce"):
        record["metadata"]["expires_at"] = expiry
        await memory._put(record)
    assert await memory.preferences("ecommerce", "1") == {}
    assert (await memory.search("ecommerce", "1", "退款 refund"))["items"] == []
    assert (await memory.search("ecommerce", "1", "duplicate refund", kind="sql_experience", error_category="semantic", tables=["orders"]))["items"] == []


async def test_preference_schema_version_is_respected(scenario):
    memory = service()
    await memory.seed(scenario)
    await memory.update("ecommerce", "preference:group", active=True)
    assert await memory.preferences("ecommerce", "1") == {"group_by": "channel"}
    assert await memory.preferences("ecommerce", "2") == {}


def test_seed_validates_and_never_stores_fixture_sql(scenario):
    async def check():
        memory = service()
        assert (await memory.seed(scenario))["seeded"] == 3
        records = await memory.list("ecommerce")
        assert len(records) == 3
        sql = next(record for record in records if record["kind"] == "sql_experience")
        assert sql["metadata"]["verified"]
        assert "SELECT" not in str(sql)
        assert "fixture_sql" not in str(sql)
        assert await memory.preferences("ecommerce") == {}
        bad = deepcopy(scenario)
        bad["repair_experiences"][0]["id"] = "invalid"
        bad["repair_experiences"][0]["expected"] = [[999]]
        with pytest.raises(ValueError, match="did not verify"):
            await memory.seed(bad)
    asyncio.run(check())


def test_seed_idempotent_tombstones_and_disabled_preferences(scenario):
    async def check():
        memory = service()
        await memory.seed(scenario)
        await memory.update("ecommerce", "preference:group", active=True, value="category")
        assert await memory.preferences("ecommerce") == {"group_by": "category"}
        await memory.seed(scenario)
        assert await memory.preferences("ecommerce") == {"group_by": "category"}
        await memory.update("ecommerce", "business:returns", active=False)
        await memory.delete("ecommerce", "sql_experience:duplicate")
        await memory.seed(scenario)
        records = await memory.list("ecommerce")
        assert len(records) == 2
        assert not next(r for r in records if r["id"] == "business:returns")["active"]
        assert not (await memory.search("ecommerce", "1", "refund", kind="sql_experience"))["items"]
        with pytest.raises(KeyError):
            await memory.update("ecommerce", "sql_experience:duplicate", active=True)
    asyncio.run(check())


def test_search_filters_scenario_schema_error_tables_and_hydrates(scenario):
    async def check():
        memory = service(TinyEmbedder())
        await memory.seed(scenario)
        result = await memory.search("ecommerce", "1", "refund join", "sql_experience", "semantic", ["orders"])
        assert result["mode"] == "offline_hybrid"
        assert [r["id"] for r in result["items"]] == ["sql_experience:duplicate"]
        for sid, version, category, tables in [("saas", "1", "semantic", ["orders"]), ("ecommerce", "2", "semantic", ["orders"]), ("ecommerce", "1", "syntax", ["orders"]), ("ecommerce", "1", "semantic", ["other"])]:
            assert not (await memory.search(sid, version, "refund join", "sql_experience", category, tables))["items"]
        # Simulate stale search index: canonical deletion/disable must still exclude a hit.
        record = await memory._get("ecommerce", "sql_experience:duplicate")
        record["active"] = False
        await memory._put(record)
        assert not (await memory.search("ecommerce", "1", "refund join", "sql_experience"))["items"]
    asyncio.run(check())


def test_embeddings_cached_missing_vectors_repaired_and_fallback_honest(scenario):
    async def check():
        embedder = TinyEmbedder(fail=True)
        memory = service(embedder)
        await memory.seed(scenario)
        assert memory.embedding_failures == 2
        result = await memory.search("ecommerce", "1", "refund", "business")
        assert result["mode"] == "offline_lexical_fallback" and result["items"]
        assert "private" not in str(result)
        embedder.fail = False
        before = embedder.calls
        await memory.seed(scenario)
        assert embedder.calls == before + 2
        await memory.seed(scenario)
        assert embedder.calls == before + 2
        assert (await memory.search("ecommerce", "1", "refund"))["mode"] == "offline_hybrid"
    asyncio.run(check())


def test_runtime_repair_idempotence_and_content_edit_invalidates_verification():
    async def check():
        memory = service()
        first = await memory.remember_repair("saas", "v1", "run-a", "date function cast 类型修复", "type", ["events"])
        second = await memory.remember_repair("saas", "v1", "run-a", "must not replace", "schema", ["other"])
        assert first == second
        assert len(await memory.list("saas")) == 1
        assert (await memory.search("saas", "v1", "date cast", "sql_experience"))["items"]
        await memory.update("saas", first["id"], content="new unverified strategy")
        assert not (await memory.search("saas", "v1", "strategy", "sql_experience"))["items"]
        await memory.delete("saas", first["id"])
        tombstone = await memory.remember_repair("saas", "v1", "run-a", "do not revive", "type", ["events"])
        assert tombstone["metadata"]["deleted"]
        assert not await memory.list("saas")
    asyncio.run(check())


def test_preference_direct_reads_and_cross_scenario_update_rejected(scenario):
    async def check():
        memory = service(TinyEmbedder())
        await memory.seed(scenario)
        calls = memory.embedder.calls
        await memory.update("ecommerce", "preference:group", active=True)
        assert await memory.preferences("ecommerce") == {"group_by": "channel"}
        assert memory.embedder.calls == calls
        assert await memory.preferences("saas") == {}
        with pytest.raises(KeyError):
            await memory.update("saas", "preference:group", active=True)
        with pytest.raises(ValueError, match="directly"):
            await memory.search("ecommerce", "1", "anything", "preference")
    asyncio.run(check())


def test_invalid_embedding_is_not_indexed(scenario):
    class InvalidEmbedder:
        async def embed(self, texts):
            return [[float("nan")] * 1024 for _ in texts]

    async def check():
        memory = service(InvalidEmbedder())
        await memory.seed(scenario)
        assert all(document["embedding"] is None for document in memory._documents.values())
        assert (await memory.search("ecommerce", "1", "refund"))["mode"] == "offline_lexical_fallback"
    asyncio.run(check())


def test_changed_embedding_model_cannot_search_incompatible_vectors(scenario):
    async def check():
        embedder = TinyEmbedder()
        memory = service(embedder)
        await memory.seed(scenario)
        embedder.model = "different-embedding-model"
        result = await memory.search("ecommerce", "1", "refund")
        assert result["mode"] == "offline_lexical_no_indexed_vectors"
        assert result["items"]
        await memory.seed(scenario)
        assert (await memory.search("ecommerce", "1", "refund"))["mode"] == "offline_hybrid"
    asyncio.run(check())


def test_rrf_favors_agreement_and_deduplicates():
    ranked = weighted_rrf(["a", "b", "b"], ["b", "c"], limit=3)
    assert ranked[0][0] == "b"
    assert len(ranked) == 3
