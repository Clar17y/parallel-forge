"""Publish complete controller results as immutable PostgreSQL evidence."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.application.adapters.named_check_receipts import encode_command_result
from forge.application.ports.executions import ExecutionStatus
from forge.application.ports.runner import CommandResult, CommandTerminalResult
from forge.application.services.validation import ValidationError, ValidationService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.evidence import EvidenceStatus, decode_evidence_manifest
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode, StepKind
from forge.domain.validation import command_spec_digest
from forge.persistence.models import Run
from forge.persistence.unit_of_work import PostgresUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _case(session_factory, persisted_run, tmp_path, *, exit_code=None, timed_out=True):
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    command = CommandSpec(name="unit", kind=StepKind.TEST, argv=("pytest",), timeout_seconds=10)
    policy = ProjectPolicy(
        id=persisted_run.project_id,
        version=1,
        repository_path=str(tmp_path),
        github_repository="owner/repo",
        default_branch="main",
        commands=(command,),
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
    )
    async with session_factory() as session, session.begin():
        run = await session.get(Run, persisted_run.id)
        run.state = "VALIDATING"
    step_id, result_id = uuid4(), uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        await work.controller_steps.admit(persisted_run.id, step_id, "validate", 1)
        outputs = []
        for data in (b"stdout", b"stderr"):
            descriptor = await store.put_bytes(data, media_type="application/json")
            outputs.append(
                await work.artifacts.record(
                    descriptor,
                    run_id=persisted_run.id,
                    producer_type="command_output",
                    producer_id=persisted_run.id,
                )
            )
        result = CommandResult(
            command_name="unit",
            kind=StepKind.TEST,
            command_digest=command_spec_digest(command),
            policy_version=1,
            exit_code=exit_code,
            timed_out=timed_out,
            started_at=datetime.now(UTC),
            duration_ms=20,
            stdout_digest=outputs[0].digest,
            stderr_digest=outputs[1].digest,
            runner_mode=RunnerMode.TRUSTED_HOST,
            image_digest=None,
            network_enabled=False,
            stdout_original_byte_count=6,
            stderr_original_byte_count=6,
            stdout_truncated=False,
            stderr_truncated=False,
            unsandboxed=True,
        )
        descriptor = await store.put_bytes(
            encode_command_result(result),
            media_type="application/vnd.forge.command-result+json",
        )
        await work.artifacts.record(
            descriptor,
            run_id=persisted_run.id,
            producer_type="command_result",
            producer_id=result_id,
            parent_digests=tuple(sorted(item.digest for item in outputs)),
        )
        await work.commit()
    return store, policy, step_id, result_id, result


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "cancelled", "status"),
    (
        (None, True, False, EvidenceStatus.FAILED),
        (0, False, False, EvidenceStatus.PASSED),
        (7, False, False, EvidenceStatus.FAILED),
        (0, False, True, EvidenceStatus.CANCELLED),
    ),
)
async def test_check_outcomes_and_controller_completion_are_atomic(
    session_factory,
    persisted_run,
    tmp_path,
    exit_code,
    timed_out,
    cancelled,
    status,
):
    store, policy, step_id, result_id, result = await _case(
        session_factory,
        persisted_run,
        tmp_path,
        exit_code=exit_code,
        timed_out=timed_out,
    )
    evidence_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        evidence = await ValidationService(store).publish(
            work,
            run_id=persisted_run.id,
            step_id=step_id,
            evidence_set_id=evidence_id,
            policy=policy,
            head_sha="a" * 40,
            results=(
                (result_id, CommandTerminalResult(result=result, caller_cancelled=cancelled)),
            ),
        )
        manifest = decode_evidence_manifest(await store.open_bytes(evidence.manifest_digest))
        assert manifest.members[0].status is status
        assert manifest.members[0].exit_code == exit_code
        assert manifest.members[0].command_result_digest == result.evidence_digest
        step = await work.controller_steps.get(persisted_run.id, step_id)
        assert step.status is ExecutionStatus.SUCCEEDED
        assert step.output_artifact_id == evidence.manifest_artifact_id
        # Caller rollback must remove both projections and step settlement.
    async with PostgresUnitOfWork(session_factory) as work:
        step = await work.controller_steps.get(persisted_run.id, step_id)
        assert step.status is ExecutionStatus.RUNNING
        await ValidationService(store).publish(
            work,
            run_id=persisted_run.id,
            step_id=step_id,
            evidence_set_id=evidence_id,
            policy=policy,
            head_sha="a" * 40,
            results=(
                (result_id, CommandTerminalResult(result=result, caller_cancelled=cancelled)),
            ),
        )
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        assert (
            await work.evidence.get_by_id(evidence_id, run_id=persisted_run.id)
        ).step_id == step_id


@pytest.mark.parametrize("mutation", ("missing", "duplicate", "changed_result", "foreign_producer"))
async def test_incomplete_or_unbound_results_cannot_publish(
    session_factory,
    persisted_run,
    tmp_path,
    mutation,
):
    store, policy, step_id, result_id, result = await _case(
        session_factory, persisted_run, tmp_path
    )
    terminal = CommandTerminalResult(result=result, caller_cancelled=False)
    results = ((result_id, terminal),)
    if mutation == "missing":
        results = ()
    elif mutation == "duplicate":
        results = results + results
    elif mutation == "changed_result":
        results = (
            (result_id, replace(terminal, result=replace(result, exit_code=0, timed_out=False))),
        )
    else:
        results = ((uuid4(), terminal),)
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(ValidationError):
            await ValidationService(store).publish(
                work,
                run_id=persisted_run.id,
                step_id=step_id,
                evidence_set_id=uuid4(),
                policy=policy,
                head_sha="a" * 40,
                results=results,
            )
        assert (
            await work.controller_steps.get(persisted_run.id, step_id)
        ).status is ExecutionStatus.RUNNING
