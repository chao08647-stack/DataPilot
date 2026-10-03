"""Read a run/checkpoint for local debugging; never prints connection settings."""
import argparse
import json

from dev import common_arguments, configure, runner


async def main(args):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from insight.config import Settings
    from insight.repository import Repository
    settings = Settings()
    repo = Repository(settings.postgres_uri.get_secret_value())
    run = repo.get_run(args.run_id)
    async with AsyncPostgresSaver.from_conn_string(repo.dsn) as saver:
        snapshot = await saver.aget_tuple({"configurable": {"thread_id": run["thread_id"]}})
        state = snapshot.checkpoint["channel_values"]
        print(json.dumps({"status": run["status"], **{key: state.get(key) for key in ("plan", "error", "review", "repair_history", "budget")}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    common_arguments(parser)
    parser.add_argument("run_id")
    args = parser.parse_args()
    configure(args)
    runner(main(args))
