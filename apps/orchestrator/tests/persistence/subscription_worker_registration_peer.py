"""A real Forge worker process using the operator installation loader."""

import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from forge.agents.codex_gateway import CodexCapabilityReport
from forge.domain.capability_evidence import (
    CapabilityEvidenceManifest,
    CapabilityProof,
    CapabilityProofKind,
    ResolvedCapabilityEvidence,
    capability_identity,
    encode_capability_evidence,
)
from forge.domain.subscription import TaskBudget
from forge.settings import Settings
from forge.worker import main as worker_main
from forge.worker.subscription_installations import SubscriptionVerifierDependencies
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
    if registered:

        def verify(installation, scope):
            identity = capability_identity(
                scope=scope,
                client_version="0.153.4",
                executable_digest=installation.executable_digest,
                client_home=installation.client_home,
                account=installation.account,
            )
            observed = datetime(2026, 9, 13, tzinfo=UTC)
            manifest = CapabilityEvidenceManifest(
                evidence_id=uuid5(NAMESPACE_URL, f"forge-worker-fake:{identity.digest}"),
                identity=identity,
                verifier_id="fake-worker-codex-verification",
                verifier_version="1",
                observed_at=observed,
                expires_at=observed + timedelta(hours=1),
                proofs=tuple(
                    CapabilityProof(kind=kind, artifact_digest=f"{index:x}" * 64)
                    for index, kind in enumerate(CapabilityProofKind, start=3)
                ),
            )
            wire = encode_capability_evidence(manifest)
            return CodexCapabilityReport(
                supported=True,
                installed_version="0.153.4",
                account_kind="chatgpt",
                native_tools_isolated=True,
                model=installation.model,
                effort=installation.effort,
                quota_limit_id=installation.quota_limit_id,
                client_home=installation.client_home,
                account=installation.account,
                executable_digest=installation.executable_digest,
                evidence=ResolvedCapabilityEvidence(
                    manifest=manifest,
                    artifact_digest=hashlib.sha256(wire).hexdigest(),
                    revision=1,
                ),
            )

        worker_main.production_subscription_verifiers = lambda *_args: (
            SubscriptionVerifierDependencies(codex=SimpleNamespace(verify=verify))
        )
    stop = asyncio.Event()
    worker = asyncio.create_task(
        worker_main.run_worker(settings, stop_event=stop, poll_interval=0.05)
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
