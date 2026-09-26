"""Content-bound controller validation publishes replayable command receipt lineage."""

import json
from dataclasses import replace

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.worktrees import GitSnapshotFile
from forge.application.services.recovery import RecoveryService
from forge.application.services.validation import ValidationError
from forge.domain.evidence import decode_evidence_manifest
from forge.persistence.models import EvidenceSet
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.worker.recovery_adapters import local_recovery_adapters
from sqlalchemy import select, text
from test_delivery_validation import _case, _expire_operation
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.fixture(autouse=True)
async def _clear_disposable_evidence(workflow_session_factory):
    yield
    # This database is owned by the guarded disposable migration fixture.
    async with workflow_session_factory() as session:
        await session.execute(text("TRUNCATE TABLE evidence_sets CASCADE"))
        await session.commit()


async def test_validation_binds_receipts_and_replays_the_same_candidate(
    tmp_path, workflow_session_factory
):
    case, command, service, runner = await _case(tmp_path, workflow_session_factory)
    candidate = CandidateInspection.from_snapshot(service._git_factory(None).snapshot)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        evidence = await service.execute(command, work, candidate=candidate)
    manifest = decode_evidence_manifest(
        await case.artifact_store.open_bytes(evidence.manifest_digest)
    )
    assert manifest.schema_version == evidence.manifest_schema_version == 2
    assert manifest.candidate_tree_digest == candidate.tree_digest
    assert runner.calls == ["unit", "lint"]
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        artifact = await work.artifacts.get_by_digest(evidence.manifest_digest, run_id=case.run_id)
        for member in manifest.members:
            assert member.controller_receipt_digest in artifact.parent_digests
            receipt_artifact = await work.artifacts.get_by_digest(
                member.controller_receipt_digest, run_id=case.run_id
            )
            assert receipt_artifact.schema_version == 2
            receipt = json.loads(
                await case.artifact_store.open_bytes(member.controller_receipt_digest)
            )
            assert receipt["request_payload"]["candidate_tree_digest"] == candidate.tree_digest
            assert (
                receipt["candidate_tree_digest_before"]
                == receipt["candidate_tree_digest_after"]
                == candidate.tree_digest
            )
        assert await service.execute(command, work, candidate=candidate) == evidence
    assert runner.calls == ["unit", "lint"]


def changed_snapshot(git):
    return replace(
        git.snapshot,
        files=(
            GitSnapshotFile(
                path="generated.txt",
                mode="100644",
                content_digest="e" * 64,
                byte_count=1,
            ),
        ),
        changed_paths=("generated.txt",),
    )


@pytest.mark.parametrize("when", ["before", "runner", "publication"])
async def test_same_head_content_changes_cannot_publish_validation(
    tmp_path,
    workflow_session_factory,
    when,
):
    case, command, service, runner = await _case(tmp_path, workflow_session_factory)
    git = service._git_factory(None)
    candidate = CandidateInspection.from_snapshot(git.snapshot)
    if when == "before":
        git.snapshot = changed_snapshot(git)
    elif when == "runner":
        run = runner.run_terminal

        async def drift(request):
            terminal = await run(request)
            git.snapshot = changed_snapshot(git)
            return terminal

        runner.run_terminal = drift
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        record = work.artifacts.record

        async def drift_at_publication(*args, **kwargs):
            artifact = await record(*args, **kwargs)
            if kwargs.get("producer_type") == "evidence_set" and when == "publication":
                git.snapshot = changed_snapshot(git)
            return artifact

        work.artifacts.record = drift_at_publication
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work, candidate=candidate)
    assert (
        runner.calls
        == {
            "before": [],
            "runner": ["unit"],
            "publication": ["unit", "lint"],
        }[when]
    )
    async with workflow_session_factory() as session:
        assert (
            await session.scalar(select(EvidenceSet).where(EvidenceSet.run_id == case.run_id))
            is None
        )


@pytest.mark.parametrize("startup", [False, True])
async def test_content_receipt_survives_crash_before_settlement(
    tmp_path,
    workflow_session_factory,
    startup,
    monkeypatch,
):
    case, command, service, runner = await _case(tmp_path, workflow_session_factory)
    git = service._git_factory(None)
    candidate = CandidateInspection.from_snapshot(git.snapshot)
    async with PostgresUnitOfWork(workflow_session_factory) as work:

        async def crash(*args, **kwargs):
            raise RuntimeError("crash before settlement")

        work.operations.complete = crash
        with pytest.raises(RuntimeError, match="crash before settlement"):
            await service.execute(command, work, candidate=candidate)
    assert runner.calls == ["unit"]
    await _expire_operation(workflow_session_factory, case.run_id)
    if startup:
        operations = PostgresOperationRepository(workflow_session_factory)
        adapters = local_recovery_adapters(
            workflow_session_factory,
            case.artifact_store,
            service._git_factory,
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("receipt recovery must not observe current contents")

        with monkeypatch.context() as patch:
            patch.setattr(git, "working_tree_snapshot", forbidden)
            recovered = await RecoveryService(operations).reconcile_all(adapters)
        assert len(recovered) == 1
        assert runner.calls == ["unit"]
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        evidence = await service.execute(command, work, candidate=candidate)
    assert evidence.manifest_schema_version == 2
    assert evidence.candidate_tree_digest == candidate.tree_digest
    assert runner.calls == ["unit", "lint"]


@pytest.mark.parametrize("change", ["omit", "new_target", "contents"])
async def test_replay_cannot_change_its_admitted_content_authority(
    tmp_path,
    workflow_session_factory,
    change,
):
    _data, command, service, runner = await _case(tmp_path, workflow_session_factory)
    git = service._git_factory(None)
    candidate = CandidateInspection.from_snapshot(git.snapshot)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(command, work, candidate=candidate)
    if change != "omit":
        git.snapshot = changed_snapshot(git)
    requested = (
        None
        if change == "omit"
        else CandidateInspection.from_snapshot(git.snapshot)
        if change == "new_target"
        else candidate
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CommandRecoveryRequired):
            await service.execute(command, work, candidate=requested)
    assert runner.calls == ["unit", "lint"]


async def test_publication_cannot_upgrade_head_only_results_to_candidate_evidence(
    tmp_path,
    workflow_session_factory,
):
    _data, command, service, runner = await _case(tmp_path, workflow_session_factory)
    candidate = CandidateInspection.from_snapshot(service._git_factory(None).snapshot)
    publish = service.publish
    arguments = {}

    async def captured(work, **kwargs):
        arguments.update(kwargs)
        return await publish(work, **kwargs)

    service.publish = captured
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        legacy = await service.execute(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(ValidationError, match="controller receipt differs"):
            await publish(work, **arguments | {"candidate": candidate})
    assert legacy.manifest_schema_version == 1
    assert runner.calls == ["unit", "lint"]


@pytest.mark.parametrize("change", ["missing", "schema"])
async def test_publication_reverifies_the_content_receipt_graph(
    tmp_path,
    workflow_session_factory,
    change,
):
    case, command, service, runner = await _case(tmp_path, workflow_session_factory)
    candidate = CandidateInspection.from_snapshot(service._git_factory(None).snapshot)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        get = work.artifacts.get_by_producer

        async def substitute(**kwargs):
            records = await get(**kwargs)
            if kwargs.get("producer_type") == "controller_named_check":
                return (
                    ()
                    if change == "missing"
                    else tuple(replace(record, schema_version=1) for record in records)
                )
            return records

        work.artifacts.get_by_producer = substitute
        with pytest.raises(ValidationError, match="controller receipt differs"):
            await service.execute(command, work, candidate=candidate)
    assert runner.calls == ["unit", "lint"]
    async with workflow_session_factory() as session:
        assert (
            await session.scalar(select(EvidenceSet).where(EvidenceSet.run_id == case.run_id))
            is None
        )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        assert (
            await service.execute(command, work, candidate=candidate)
        ).manifest_schema_version == 2
    assert runner.calls == ["unit", "lint"]
