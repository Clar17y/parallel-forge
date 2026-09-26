"""Controller execution and recovery bind actual candidate contents, not HEAD alone."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from forge.application.adapters.controller_check import (
    CONTROLLER_CHECK_KIND,
    ControllerCheckOperationAdapter,
    ControllerCheckOperationError,
)
from forge.application.ports.worktrees import GitSnapshotFile
from forge.domain.operation import OperationStatus
from test_controller_check_adapter import _setup_real_adapter, _Unused


def recovery(adapter, **overrides):
    return ControllerCheckOperationAdapter.for_recovery(
        **{
            "run_id": adapter._run_id,
            "step_id": adapter._step_id,
            "result_id": adapter._result_id,
            "worktree": adapter._worktree,
            "policy": adapter._policy,
            "command_name": adapter._command_name,
            "head_sha": adapter._head_sha,
            "candidate_tree_digest": adapter._candidate_tree_digest,
            "artifacts": adapter._artifacts,
            "artifact_store": adapter._store,
        }
        | overrides
    )


@pytest.mark.integration
async def test_candidate_receipt_replays_without_git_or_another_launch(
    tmp_path, persisted_run, session_factory
):
    adapter, intent, factory, _, _ = await _setup_real_adapter(
        tmp_path, persisted_run, session_factory, candidate_bound=True
    )
    result = await adapter.invoke(intent)
    wire = await adapter._store.open_bytes(result.payload["receipt_digest"])
    receipt = json.loads(wire)
    assert receipt["receipt_version"] == 2
    assert (
        receipt["candidate_tree_digest_before"]
        == receipt["candidate_tree_digest_after"]
        == adapter._candidate_tree_digest
    )
    recovered = recovery(adapter)
    recovered._git = recovered._factory = _Unused()
    assert await recovered.reconcile(intent) == result
    assert factory.calls == factory.runner.calls == 1


@pytest.mark.integration
@pytest.mark.parametrize("when", ["before", "after"])
async def test_same_head_content_drift_never_yields_controller_receipt(
    tmp_path, persisted_run, session_factory, when
):
    adapter, intent, factory, _, _ = await _setup_real_adapter(
        tmp_path, persisted_run, session_factory, candidate_bound=True
    )
    git = adapter._git
    changed = replace(
        git.snapshot,
        files=(
            GitSnapshotFile(
                path="generated.txt",
                mode="100644",
                content_digest="e" * 64,
                byte_count=1,
            ),
        ),
    )
    if when == "before":
        git.snapshot = changed
    else:
        run = factory.runner.run_terminal

        async def drift(request):
            terminal = await run(request)
            git.snapshot = changed
            return terminal

        factory.runner.run_terminal = drift
    with pytest.raises(ControllerCheckOperationError, match="candidate contents changed"):
        await adapter.invoke(intent)
    assert factory.calls == (when == "after")
    assert not await adapter._artifacts.get_by_producer(
        run_id=intent.run_id,
        producer_type=CONTROLLER_CHECK_KIND,
        producer_id=adapter._result_id,
    )
    assert (
        await recovery(adapter).reconcile(intent)
    ).status is OperationStatus.NEEDS_RECONCILIATION


@pytest.mark.integration
@pytest.mark.parametrize("change", ["missing", "before", "after", "downgrade", "schema_alias"])
async def test_recovery_requires_both_bound_observations(
    tmp_path, persisted_run, session_factory, change
):
    adapter, intent, _, _, _ = await _setup_real_adapter(
        tmp_path, persisted_run, session_factory, candidate_bound=True
    )
    outcome = await adapter.invoke(intent)
    receipt = json.loads(await adapter._store.open_bytes(outcome.payload["receipt_digest"]))
    if change == "missing":
        receipt.pop("candidate_tree_digest_after")
    elif change in ("before", "after"):
        receipt[f"candidate_tree_digest_{change}"] = "f" * 64
    elif change == "downgrade":
        receipt["receipt_version"] = 1
        receipt.pop("candidate_tree_digest_before")
        receipt.pop("candidate_tree_digest_after")
    else:
        receipt["receipt_version"] = 2.0
    wire = json.dumps(receipt, separators=(",", ":"), sort_keys=True).encode()
    descriptor = await adapter._store.put_bytes(
        wire, media_type="application/vnd.forge.controller-check-receipt+json"
    )
    (original,) = await adapter._artifacts.get_by_producer(
        run_id=intent.run_id,
        producer_type=CONTROLLER_CHECK_KIND,
        producer_id=adapter._result_id,
    )
    descriptor = replace(
        original,
        digest=descriptor.digest,
        storage_path=descriptor.storage_path,
        byte_count=len(wire),
        original_byte_count=len(wire),
    )

    async def substituted(**kwargs):
        return (descriptor,)

    reader = recovery(
        adapter,
        artifacts=SimpleNamespace(
            get_by_producer=substituted,
            get_by_digest=adapter._artifacts.get_by_digest,
        ),
    )
    assert (await reader.reconcile(intent)).status is OperationStatus.NEEDS_RECONCILIATION


@pytest.mark.integration
async def test_candidate_recovery_cannot_use_head_only_receipt(
    tmp_path, persisted_run, session_factory
):
    adapter, intent, _, _, _ = await _setup_real_adapter(tmp_path, persisted_run, session_factory)
    await adapter.invoke(intent)
    reader = recovery(adapter, candidate_tree_digest=adapter._git.snapshot.candidate_tree_digest)
    assert (await reader.reconcile(intent)).status is OperationStatus.NEEDS_RECONCILIATION
