"""Explicit opt-in real model probes. No secrets/endpoint addresses in output."""

import argparse
import json
import time

from dev import common_arguments, configure, runner
from pydantic import BaseModel


class Ping(BaseModel):
    status: str


async def main():
    from insight.config import Settings
    from insight.providers import Embedder, ModelClient
    settings = Settings()
    report = {"llm_model": settings.llm_model, "embedding_model": settings.embedding_model}
    budget = {}
    started = time.perf_counter()
    response = await ModelClient(settings).complete(Ping, "连通性测试，只返回status=ok的JSON。", {"synthetic_probe": True}, budget)
    report["llm"] = {"passed": response.status == "ok", "elapsed_seconds": round(time.perf_counter()-started, 3), "usage": budget}
    started = time.perf_counter()
    vectors = await Embedder(settings).embed(["合成电商数据：订单退款按订单先聚合，防止重复统计。", "合成SaaS数据：MRR按月归一化订阅收入。"])
    report["embedding"] = {"passed": len(vectors) == 2, "count": len(vectors), "dimensions": len(vectors[0]), "elapsed_seconds": round(time.perf_counter()-started, 3)}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    common_arguments(parser)
    parser.add_argument("--live", action="store_true", required=True, help="Explicitly authorize these bounded real service calls")
    args = parser.parse_args()
    configure(args)
    runner(main())
