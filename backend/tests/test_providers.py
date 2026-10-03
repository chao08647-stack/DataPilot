"""Provider boundary tests: all HTTP is MockTransport; real network is forbidden."""

import json
import traceback

import httpx
import pytest
from pydantic import BaseModel

from insight import providers
from insight.config import Settings
from insight.providers import Embedder, ModelClient, ModelError


class Reply(BaseModel):
    message: str
    count: int


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    async def reject_async(*args, **kwargs):
        raise AssertionError("Provider tests must not perform real network requests.")

    def reject_sync(*args, **kwargs):
        raise AssertionError("Provider tests must not perform real network requests.")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_sync)


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        llm_base_url="https://llm-unit-test.invalid/v1",
        llm_api_key="test-only-llm-secret",
        llm_model="unit-test-model",
        embedding_base_url="https://embedding-unit-test.invalid/v1",
        embedding_api_key="test-only-embedding-secret",
        embedding_model="bge-m3",
        postgres_uri="",
        postgres_host="",
        max_model_calls=4,
    )


@pytest.fixture
def mock_http(monkeypatch):
    original_client = httpx.AsyncClient
    requests = []
    sleeps = []

    async def no_wait(delay):
        sleeps.append(delay)

    monkeypatch.setattr(providers.asyncio, "sleep", no_wait)

    def install(handler):
        def record(request):
            requests.append(request)
            return handler(request)

        def client_factory(**kwargs):
            return original_client(transport=httpx.MockTransport(record), trust_env=False, **kwargs)

        monkeypatch.setattr(providers.httpx, "AsyncClient", client_factory)
        return requests, sleeps

    return install


def completion(content='{"message":"完成","count":2}', **extra):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 5},
            **extra,
        },
    )


def vector(value=0.25):
    return [value] + [0.0] * 1023


def embedding_response(items):
    # Deliberately permit wire-level NaN/Infinity for rejection tests; Response(json=...)
    # itself rejects those values and would never exercise the adapter validation.
    return httpx.Response(
        200,
        content=json.dumps({"data": items}, allow_nan=True).encode(),
        headers={"content-type": "application/json"},
    )


async def test_llm_normal_json_and_usage_budget(settings, mock_http):
    requests, _ = mock_http(lambda request: completion())
    budget = {}
    value = await ModelClient(settings).complete(Reply, "只根据给定证据回答", {"question": "统计订单"}, budget)
    assert value == Reply(message="完成", count=2)
    assert budget["calls"] == 1
    assert budget["input_tokens"] == 11
    assert budget["output_tokens"] == 5
    assert budget["duration_ms"] >= 0
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer test-only-llm-secret"
    body = json.loads(requests[0].content)
    assert body["model"] == "unit-test-model"
    assert body["response_format"] == {"type": "json_object"}
    assert json.loads(body["messages"][1]["content"]) == {"question": "统计订单"}


async def test_final_review_reasoning_is_scoped_and_budgeted(settings, mock_http):
    settings.llm_model = "deepseek-test"
    requests, _ = mock_http(lambda request: completion())
    budget = {}
    client = ModelClient(settings)
    await client.complete(Reply, "system", {}, budget)
    await client.complete(Reply, "system", {"review_phase": "delivery"}, budget)
    ordinary, review = (json.loads(request.content) for request in requests)
    assert ordinary["thinking"]["type"] == "disabled"
    assert review["thinking"]["type"] == "enabled"
    assert review["reasoning_effort"] == "low"
    assert review["max_tokens"] == settings.review_max_output_tokens
    assert budget["calls"] == 2 and budget["reasoning_calls"] == 1


async def test_token_truncation_is_not_retried_unchanged(settings, mock_http):
    requests, _ = mock_http(lambda request: completion(choices=[{"finish_reason": "length", "message": {"content": "{"}}]))
    budget = {}
    with pytest.raises(ModelError, match="Token上限"):
        await ModelClient(settings).complete(Reply, "system", {}, budget)
    assert budget["calls"] == len(requests) == 1
    assert budget["duration_ms"] >= 0 and budget["output_tokens"] == 5


async def test_analysis_reasoning_is_explicit_optional_and_bounded(settings, mock_http):
    settings.llm_model = 'deepseek-test'
    requests, _ = mock_http(lambda request: completion())
    budget = {}
    client = ModelClient(settings)
    await client.complete(Reply, 'system', {'analysis_phase': 'evidence_interpretation'}, budget)
    enabled = json.loads(requests[0].content)
    assert enabled['thinking']['type'] == 'enabled'
    assert enabled['reasoning_effort'] == 'low'
    assert enabled['max_tokens'] == settings.review_max_output_tokens
    settings.llm_analysis_thinking = False
    await client.complete(Reply, 'system', {'analysis_phase': 'evidence_interpretation'}, budget)
    assert json.loads(requests[1].content)['thinking']['type'] == 'disabled'
    assert budget['calls'] == 2 and budget['reasoning_calls'] == 1


async def test_invalid_evidence_id_is_corrected_within_single_bounded_adapter_call(settings, mock_http):
    from insight.models import Analysis
    replies = iter([
        {'summary': '结果', 'findings': [{'title': '留存', 'detail': '已有证据', 'evidence_ids': ['retention_summary']}]},
        {'summary': '结果', 'findings': [{'title': '留存', 'detail': '已有证据', 'evidence_ids': ['calc0_retention_retention_summary']}]},
    ])
    requests, _ = mock_http(lambda request: completion(json.dumps(next(replies))))
    budget = {}
    result = await ModelClient(settings).complete(Analysis, 'system',
        {'queries': [{'id': 'calc0_retention_retention_summary'}]}, budget)
    assert result.findings[0].evidence_ids == ['calc0_retention_retention_summary']
    assert budget['calls'] == len(requests) == 2
    content = json.loads(requests[0].content)['messages'][0]['content']
    assert '"enum": ["calc0_retention_retention_summary"]' in content


async def test_repeated_invalid_evidence_is_rejected_not_aliased(settings, mock_http):
    from insight.models import Analysis
    requests, _ = mock_http(lambda request: completion(json.dumps({'summary': 'x', 'findings': [
        {'title': 'x', 'detail': 'x', 'evidence_ids': ['invented']}]})))
    budget = {}
    with pytest.raises(ModelError, match='结构化校验'):
        await ModelClient(settings).complete(Analysis, 'system', {'queries': [{'id': 'real'}]}, budget)
    assert budget['calls'] == len(requests) == 2


@pytest.mark.parametrize("invalid", ["not JSON", '{"message":"缺少 count"}', '{"message":{},"count":2}'])
async def test_llm_bad_format_retries_once_and_counts_every_attempt(settings, mock_http, invalid):
    responses = iter([completion(invalid), completion()])
    requests, _ = mock_http(lambda request: next(responses))
    budget = {}
    result = await ModelClient(settings).complete(Reply, "system", {}, budget)
    assert result.count == 2
    assert len(requests) == budget["calls"] == 2
    assert budget["input_tokens"] == 22
    assert budget["output_tokens"] == 10
    retry_body = json.loads(requests[1].content)
    assert "上次格式不符合要求" in retry_body["messages"][0]["content"]


async def test_llm_repeated_bad_json_stops_after_two_requests(settings, mock_http):
    requests, _ = mock_http(lambda request: completion("invalid"))
    budget = {}
    with pytest.raises(ModelError, match="结构化校验"):
        await ModelClient(settings).complete(Reply, "system", {}, budget)
    assert len(requests) == budget["calls"] == 2


async def test_llm_missing_choice_is_structured_failure_not_indexerror(settings, mock_http):
    requests, _ = mock_http(lambda request: httpx.Response(200, json={"choices": []}))
    budget = {}
    with pytest.raises(ModelError, match="结构化校验"):
        await ModelClient(settings).complete(Reply, "system", {}, budget)
    assert len(requests) == budget["calls"] == 2


async def test_llm_exhausted_budget_sends_no_request(settings, mock_http):
    requests, _ = mock_http(lambda request: completion())
    settings.max_model_calls = 1
    budget = {"calls": 1}
    with pytest.raises(ModelError, match="预算已耗尽"):
        await ModelClient(settings).complete(Reply, "system", {}, budget)
    assert requests == []
    assert budget["calls"] == 1


async def test_llm_format_retry_cannot_bypass_one_call_budget(settings, mock_http):
    requests, _ = mock_http(lambda request: completion("invalid"))
    settings.max_model_calls = 1
    budget = {}
    with pytest.raises(ModelError, match="预算已耗尽"):
        await ModelClient(settings).complete(Reply, "system", {}, budget)
    assert len(requests) == budget["calls"] == 1


async def test_llm_transient_503_then_success(settings, mock_http):
    responses = iter([httpx.Response(503, text="upstream unavailable"), completion()])
    requests, sleeps = mock_http(lambda request: next(responses))
    budget = {}
    assert (await ModelClient(settings).complete(Reply, "system", {}, budget)).count == 2
    assert len(requests) == budget["calls"] == 2
    assert len(sleeps) == 1


@pytest.mark.parametrize("status", [429, 502, 503, 504])
async def test_llm_repeated_retryable_http_failure_is_bounded(settings, mock_http, status):
    requests, sleeps = mock_http(lambda request: httpx.Response(status, text="private upstream details"))
    budget = {}
    with pytest.raises(ModelError, match=f"HTTP {status}") as error:
        await ModelClient(settings).complete(Reply, "system", {}, budget)
    assert len(requests) == budget["calls"] == 2
    assert len(sleeps) == 1
    assert "private upstream details" not in str(error.value)


async def test_llm_unauthorized_is_not_retried(settings, mock_http):
    requests, sleeps = mock_http(lambda request: httpx.Response(401, text="authorization denied"))
    budget = {}
    with pytest.raises(ModelError, match="HTTP 401"):
        await ModelClient(settings).complete(Reply, "system", {}, budget)
    assert len(requests) == budget["calls"] == 1
    assert sleeps == []


@pytest.mark.parametrize("kind", ["llm", "embedding"])
async def test_network_errors_do_not_disclose_endpoint_or_key(settings, mock_http, kind):
    endpoint = settings.llm_base_url if kind == "llm" else settings.embedding_base_url
    key = (settings.llm_api_key if kind == "llm" else settings.embedding_api_key).get_secret_value()

    def failure(request):
        raise httpx.ReadTimeout(f"connection to {endpoint} failed with key={key}", request=request)

    requests, _ = mock_http(failure)
    with pytest.raises(ModelError) as error:
        if kind == "llm":
            await ModelClient(settings).complete(Reply, "system", {}, {})
        else:
            await Embedder(settings).embed(["测试文本"])
    public_trace = "".join(traceback.format_exception(error.value))
    assert endpoint not in public_trace
    assert key not in public_trace
    assert len(requests) == 1


async def test_embedding_matches_input_order_and_validates_1024_dimensions(settings, mock_http):
    requests, _ = mock_http(lambda request: embedding_response([
        {"index": 1, "embedding": vector(0.75)},
        {"index": 0, "embedding": vector(0.25)},
    ]))
    result = await Embedder(settings).embed(["first", "second"])
    assert result == [vector(0.25), vector(0.75)]
    assert all(len(item) == 1024 for item in result)
    assert len(requests) == 1
    assert json.loads(requests[0].content)["input"] == ["first", "second"]


@pytest.mark.parametrize("count", [0, 1, 3])
async def test_embedding_rejects_returned_count_mismatch(settings, mock_http, count):
    mock_http(lambda request: embedding_response([{"index": index, "embedding": vector()} for index in range(count)]))
    with pytest.raises(ModelError, match="向量无效"):
        await Embedder(settings).embed(["first", "second"])


@pytest.mark.parametrize("invalid_vector", [
    [0.25] * 1023,
    [0.25] * 1025,
    [0.0] * 1024,
    [float("nan")] + [0.0] * 1023,
    [float("inf")] + [0.0] * 1023,
    [float("-inf")] + [0.0] * 1023,
    ["0.25"] + [0.0] * 1023,
], ids=["short", "long", "zero", "nan", "infinity", "negative-infinity", "not-number"])
async def test_embedding_rejects_invalid_vectors(settings, mock_http, invalid_vector):
    mock_http(lambda request: embedding_response([{"index": 0, "embedding": invalid_vector}]))
    with pytest.raises(ModelError, match="向量无效"):
        await Embedder(settings).embed(["test"])


@pytest.mark.parametrize("indexes", [[0, 0], [1, 1], [0, 2], [-1, 0], [1, 2]])
async def test_embedding_rejects_duplicate_missing_or_out_of_range_indexes(settings, mock_http, indexes):
    mock_http(lambda request: embedding_response([{"index": index, "embedding": vector()} for index in indexes]))
    with pytest.raises(ModelError, match="向量无效"):
        await Embedder(settings).embed(["first", "second"])


async def test_embedding_rejects_missing_index_field(settings, mock_http):
    mock_http(lambda request: embedding_response([{"embedding": vector()}]))
    with pytest.raises(ModelError, match="向量无效"):
        await Embedder(settings).embed(["test"])


async def test_embedding_empty_input_needs_no_http_request(settings, mock_http):
    requests, _ = mock_http(lambda request: embedding_response([]))
    assert await Embedder(settings).embed([]) == []
    assert requests == []


async def test_embedding_batches_33_inputs_without_losing_order(settings, mock_http):
    batch_count = 0

    def respond(request):
        nonlocal batch_count
        batch_count += 1
        inputs = json.loads(request.content)["input"]
        return embedding_response([
            {"index": index, "embedding": vector(float(batch_count))}
            for index in reversed(range(len(inputs)))
        ])

    requests, _ = mock_http(respond)
    result = await Embedder(settings).embed([f"text-{index}" for index in range(33)])
    assert [len(json.loads(request.content)["input"]) for request in requests] == [32, 1]
    assert len(result) == 33
    assert result[:32] == [vector(1.0)] * 32
    assert result[32] == vector(2.0)
