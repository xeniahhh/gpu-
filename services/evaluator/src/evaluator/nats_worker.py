from __future__ import annotations

import asyncio
import json
import os

import nats

from evaluator.worker import evaluate_shard


async def run_worker() -> None:
    url = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
    connection = await nats.connect(url, name="fabric-evaluator")

    async def handle(message: nats.aio.msg.Msg) -> None:
        try:
            payload = json.loads(message.data.decode("utf-8"))
            result = evaluate_shard(payload)
            await message.respond(json.dumps(result).encode("utf-8"))
        except Exception as exc:
            await message.respond(
                json.dumps({"error": str(exc)}).encode("utf-8")
            )

    await connection.subscribe(
        "fabric.eval.evaluate",
        queue="fabric-evaluators",
        cb=handle,
    )
    await asyncio.Event().wait()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
