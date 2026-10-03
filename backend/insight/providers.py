"""Small JSON model adapters. Requests contain only explicitly assembled task data."""

import asyncio
import json
import math
import time
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from insight.config import Settings

T = TypeVar("T", bound=BaseModel)


class ModelError(RuntimeError):
    pass


class BudgetExhausted(ModelError):
    pass


class ModelClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def complete(self, schema: type[T], system: str, payload: dict, budget: dict) -> T:
        if not self.settings.llm_configured:
            raise ModelError("LLM 未配置；已保存历史仍可查看，新的分析需要先配置模型。")
        budget["model"] = self.settings.llm_model
        header = {"Content-Type": "application/json"}
        key = self.settings.llm_api_key.get_secret_value()
        if key:
            header["Authorization"] = f"Bearer {key}"
        allowed_ids = sorted({q["id"] for q in payload.get("queries", []) if isinstance(q, dict) and isinstance(q.get("id"), str)})
        response_schema = schema.model_json_schema()
        def constrain(value):
            if not isinstance(value, dict):
                return
            properties = value.get("properties", {})
            if allowed_ids and "evidence_ids" in properties:
                properties["evidence_ids"]["items"] = {"type": "string", "enum": allowed_ids}
            if allowed_ids and "query_id" in properties:
                properties["query_id"]["enum"] = allowed_ids
            for child in value.values():
                if isinstance(child, dict):
                    constrain(child)
        constrain(response_schema)
        def check_references(value):
            if isinstance(value, dict):
                for name, item in value.items():
                    if allowed_ids and name == "evidence_ids" and not set(item).issubset(allowed_ids):
                        raise ValueError("unknown evidence reference")
                    if allowed_ids and name == "query_id" and item not in allowed_ids:
                        raise ValueError("unknown query reference")
                    check_references(item)
            elif isinstance(value, list):
                for item in value:
                    check_references(item)
        body = {
            "model": self.settings.llm_model,
            "temperature": 0,
            "max_tokens": self.settings.max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system + "\n只返回满足此 JSON Schema 的 JSON 对象：" + json.dumps(response_schema, ensure_ascii=False)},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
            ],
        }
        if "deepseek" in self.settings.llm_model.lower():
            review_reasoning = self.settings.llm_review_thinking and payload.get("review_phase") == "delivery"
            analysis_reasoning = self.settings.llm_analysis_thinking and payload.get("analysis_phase") == "evidence_interpretation"
            body["thinking"] = {"type": "enabled" if self.settings.llm_thinking or review_reasoning or analysis_reasoning else "disabled"}
            if review_reasoning or analysis_reasoning:
                body["max_tokens"] = self.settings.review_max_output_tokens
                body["reasoning_effort"] = self.settings.llm_review_effort
        async with httpx.AsyncClient(timeout=self.settings.model_timeout) as client:
            for attempt in range(2):
                if budget.get("calls", 0) >= self.settings.max_model_calls:
                    raise BudgetExhausted("本次任务的模型调用预算已耗尽。")
                budget["calls"] = budget.get("calls", 0) + 1
                if body.get("thinking", {}).get("type") == "enabled":
                    budget["reasoning_calls"] = budget.get("reasoning_calls", 0) + 1
                started = time.perf_counter()
                try:
                    response = await client.post(
                        self.settings.llm_base_url.rstrip("/") + "/chat/completions",
                        headers=header, json=body,
                    )
                    response.raise_for_status()
                    result = response.json()
                    usage = result.get("usage", {})
                    budget["input_tokens"] = budget.get("input_tokens", 0) + usage.get("prompt_tokens", 0)
                    budget["output_tokens"] = budget.get("output_tokens", 0) + usage.get("completion_tokens", 0)
                    if result["choices"][0].get("finish_reason") == "length":
                        raise ModelError("模型输出达到本次Token上限，未形成完整结构化结果；请降低推理强度或调整输出预算，不自动重复同一截断请求。")
                    raw = result["choices"][0]["message"]["content"]
                    if isinstance(raw, str) and raw.startswith("```"):
                        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
                    value = schema.model_validate_json(raw)
                    check_references(value.model_dump())
                    return value
                except (ValidationError, KeyError, ValueError, TypeError, IndexError):
                    if attempt:
                        raise ModelError("模型返回内容未通过结构化校验。") from None
                    body["messages"][0]["content"] += "\n上次格式不符合要求。请输出完整且简短的 JSON。"
                    if allowed_ids:
                        body["messages"][0]["content"] += "查询证据引用必须严格选择完整ID，不可引用工具名、表别名或省略前缀：" + json.dumps(allowed_ids, ensure_ascii=False)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (429, 502, 503, 504) and not attempt:
                        await asyncio.sleep(0.5)
                        continue
                    raise ModelError(f"模型服务返回 HTTP {exc.response.status_code}；请检查服务或凭据。") from None
                except httpx.HTTPError:
                    raise ModelError("模型服务连接失败或超时；未使用固定答案替代。") from None
                finally:
                    budget["duration_ms"] = budget.get("duration_ms", 0) + round((time.perf_counter() - started) * 1000)
        raise ModelError("模型调用失败。")


class Embedder:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = settings.embedding_model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.settings.embedding_base_url:
            raise ModelError("Embedding 未配置。")
        headers = {"Content-Type": "application/json"}
        key = self.settings.embedding_api_key.get_secret_value()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        vectors = []
        async with httpx.AsyncClient(timeout=self.settings.model_timeout) as client:
            for offset in range(0, len(texts), 32):
                batch = texts[offset:offset + 32]
                try:
                    response = await client.post(
                        self.settings.embedding_base_url.rstrip("/") + "/embeddings",
                        headers=headers, json={"model": self.model, "input": batch},
                    )
                    response.raise_for_status()
                    items = response.json()["data"]
                    indexes = [item["index"] for item in items]
                    if len(items) != len(batch) or any(type(index) is not int for index in indexes) or sorted(indexes) != list(range(len(batch))):
                        raise ValueError("count")
                    items = sorted(items, key=lambda item: item["index"])
                    for item in items:
                        vector = item["embedding"]
                        if len(vector) != 1024 or not all(type(x) in (int, float) and math.isfinite(x) for x in vector) or not any(vector):
                            raise ValueError("vector")
                        vectors.append(vector)
                except (httpx.HTTPError, KeyError, ValueError, TypeError):
                    raise ModelError("Embedding 调用失败或返回向量无效（需要 1024 维）。") from None
        return vectors
