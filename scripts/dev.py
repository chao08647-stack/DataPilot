"""Local backend entry point. Optional explicit model-only configuration injection."""

import argparse
import asyncio
import json
import os
import sys
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

MODEL_KEYS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL")


def configure(args):
    if getattr(args, "model_env", None):
        # Only six whitelisted values are selected. No old application module is imported.
        from dotenv import dotenv_values
        selected_lines = []
        with args.model_env.open(encoding="utf-8-sig") as source:
            for line in source:
                name = line.split("=", 1)[0].strip().removeprefix("export ").strip()
                if name in MODEL_KEYS:
                    selected_lines.append(line)
        # Parse only model entries; unrelated credentials are not retained or interpolated.
        values = dotenv_values(stream=StringIO("".join(selected_lines)), interpolate=False)
        for name in MODEL_KEYS:
            if values.get(name):
                os.environ[f"INSIGHT_{name}"] = values[name]
    if getattr(args, "local_postgres", False):
        local = json.loads((ROOT / ".runtime" / "local-postgres.json").read_text(encoding="utf-8"))
        from psycopg.conninfo import make_conninfo
        os.environ["INSIGHT_POSTGRES_URI"] = make_conninfo(host="127.0.0.1", port=15432, user="insight", dbname="insight_agents", password=local["password"])


def runner(coro):
    # Psycopg's async implementation needs SelectorEventLoop on Windows.
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
    with asyncio.Runner(loop_factory=factory) as loop:
        return loop.run(coro)


def common_arguments(parser):
    parser.add_argument("--model-env", type=Path, help="Explicit env file; read ONLY model connection keys, never copied to disk")
    parser.add_argument("--local-postgres", action="store_true", help="Use this project's locally initialized PostgreSQL")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    common_arguments(parser)
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    configure(args)
    import uvicorn

    from insight.main import create_app
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=args.port, loop="none", access_log=False))
    runner(server.serve())
