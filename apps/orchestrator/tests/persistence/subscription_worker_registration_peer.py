"""A real Forge worker process with explicitly fake Codex/capability dependencies."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from forge.agents.codex_gateway import CodexCapabilityReport, CodexInstallation
from forge.agents.codex_runtime import CodexRuntimeAdapter
from forge.domain.subscription import TaskBudget
from forge.settings import Settings
from forge.worker.main import run_worker
from sqlalchemy.engine import make_url


async def main():
    root = Path(sys.argv[1]).resolve(strict=True)
    registered = sys.argv[2] == "registered"
    assert sys.argv[2] in {"registered", "empty"}
    assert sys.version_info[:2] == (3, 14)
    settings = Settings(
        process_role="worker",
        data_root=root,
        prompt_root=Path(__file__).resolve().parents[4] / "agents",
        provider_secret_reference="",
        google_api_key_reference="",
        subscription_worker_concurrency=2,
        subscription_attempt_budget=TaskBudget(
            max_duration_seconds=30,
            max_tool_calls=5,
            max_named_checks=1,
            max_provider_attempts=1,
            max_repairs=0,
        ),
        _env_file=None,
    )
    database = make_url(settings.database_url).database
    assert database and database.startswith("forge_test_") and len(database) == 43
    assert all(char in "0123456789abcdef" for char in database[11:])
    adapters = ()
    if registered:
        installation = CodexInstallation(
            executable=sys.executable,
            cwd=str(root / "isolated-client"),
            client_home=str(root / "client-home"),
            model="gpt-6-astra",
            effort="low",
            script=(
                str(Path(__file__).parents[1] / "agents/codex_notification_peer.py"),
                "plan",
                "gpt-6-astra",
                "low",
                "0",
            ),
            duration_seconds=10,
        )
        report = CodexCapabilityReport(
            supported=True,
            installed_version="0.153.4",
            account_kind="chatgpt",
            billing_allowance_enforced=True,
            native_tools_isolated=True,
            model=installation.model,
            effort=installation.effort,
            client_home=installation.client_home,
        )
        adapters = (CodexRuntimeAdapter(installation, SimpleNamespace(verify=lambda _: report)),)
    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_worker(settings, subscription_adapters=adapters, stop_event=stop, poll_interval=0.05)
    )
    print(json.dumps({"operation": "worker-starting", "registered": registered}), flush=True)
    incoming = asyncio.create_task(asyncio.to_thread(sys.stdin.readline))
    try:
        done, _ = await asyncio.wait({worker, incoming}, return_when=asyncio.FIRST_COMPLETED)
        if incoming in done:
            assert json.loads(incoming.result()) == {"operation": "stop"}
            stop.set()
        await worker
        print(json.dumps({"operation": "worker-stopped", "settled": True}), flush=True)
    finally:
        stop.set()
        incoming.cancel()
        await asyncio.gather(incoming, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
