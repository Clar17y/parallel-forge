"""Providerless cross-process discovery crash/recovery fixture."""

import asyncio
import os

from forge.persistence.database import create_engine, create_session_factory
from forge.worker.epic_brainstorm import EpicBrainstormWorker


class Reader:
    def excludes_paths(self, paths):
        return False


class ReadThenWait:
    async def execute(self, job, turns, reader, *, cancelled, lifecycle):
        await reader.excludes_paths(("README.md",))
        print("READ_DURABLE", flush=True)
        await asyncio.Event().wait()


async def main() -> None:
    engine = create_engine(os.environ.pop("FORGE_TEST_DSN"))
    try:
        factory = create_session_factory(engine)
        worker = EpicBrainstormWorker(
            factory,
            owner="subprocess-worker",
            lease_seconds=5,
            gateway_factory=lambda _: ReadThenWait(),
            reader_factory=lambda _: Reader(),
        )
        if os.environ["FORGE_RECOVERY_MODE"] == "read":
            await worker.run_once()
        else:
            result = await worker.reconcile_settled()
            print("RECOVERED" if result is not None else "NOT_READY", flush=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
