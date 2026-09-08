import pytest
from forge.application.services.pr_evidence import PrEvidenceValidationError
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_pr_monitoring import published
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("drift", [None, "local", "new_base", "approval"])
async def test_base_update_admission_preserves_original_evidence_and_checks_candidate(
    tmp_path, workflow_session_factory, drift
):
    factory = workflow_session_factory
    case, git, read, _, validator, _, policy, approval_id = await published(tmp_path, factory)
    target = "f" * 40
    read.bases[policy.github_repository.casefold(), "main"] = target
    if drift == "local":
        git.head = "e" * 40
    elif drift == "new_base":
        read.bases[policy.github_repository.casefold(), "main"] = "e" * 40
    elif drift == "approval":
        from datetime import UTC, datetime

        async with PostgresUnitOfWork(factory) as work:
            approval = await work.auth.get_approval(approval_id=approval_id)
            approval.invalidated_at = datetime.now(UTC)
            await work.commit()
    async with PostgresUnitOfWork(factory) as work:
        with pytest.raises(PrEvidenceValidationError):
            await validator.validate_published(work, case.run_id, approval_id)
    async with PostgresUnitOfWork(factory) as work:
        if drift:
            with pytest.raises(PrEvidenceValidationError):
                await validator.validate_for_base_update(work, case.run_id, approval_id, target)
        else:
            verified = await validator.validate_for_base_update(
                work, case.run_id, approval_id, target
            )
            assert verified.remote_base_sha == target
            assert verified.evidence.base_sha == git.worktree.base_sha
            assert verified.approved.run.base_sha == git.worktree.base_sha


@pytest.mark.parametrize(
    "blocker,repair_kind",
    [
        (None, "review"),
        (None, "validation"),
        ("resumed", "review"),
        ("exhausted", None),
        ("unsafe", None),
        ("head", None),
        ("uncertain", None),
        ("uncertain_deadline", None),
        ("adoption_uncertain", None),
        ("evidence_invalid", None),
        ("expired_before", None),
        ("replay_receipt", None),
        ("replay_command", None),
        ("replay_schema", None),
        ("startup", None),
        ("startup_paused", None),
        ("startup_bad_evidence", None),
        ("paused_before", "review"),
        ("paused_repeated", "review"),
        ("paused_after", "review"),
        ("paused_settled", None),
        ("paused_settled_receipt", None),
        ("paused_settled_renewed", None),
    ],
)
async def test_monitor_base_advance_queues_one_budgeted_update(
    tmp_path, workflow_session_factory, blocker, repair_kind, monkeypatch
):
    from dataclasses import replace

    from forge.application.services.release_monitor import ReleaseMonitor
    from forge.domain.github import MergeProtection
    from forge.domain.run import RunState

    factory = workflow_session_factory
    case, git, read, writes, validator, poll, policy, approval_id = await published(
        tmp_path, factory
    )
    if blocker == "resumed":
        from test_release_monitor_resume import resumed_poll

        poll, _ = await resumed_poll(case, poll, factory)
        blocker = None
    target = "f" * 40
    repository = policy.github_repository
    read.bases[repository.casefold(), "main"] = target
    writes.pull_requests[repository, 1] = replace(
        writes.pull_requests[repository, 1], base_sha=target
    )
    read.merge_protections[repository.casefold(), "main"] = MergeProtection(
        True, False, False, "strict", required_check_names=("ci",)
    )
    if blocker == "exhausted":
        from forge.persistence.models import Run

        async with PostgresUnitOfWork(factory) as work:
            row = await work.session.get(Run, case.run_id)
            row.remote_remediation_count = policy.remote_remediation_limit
            await work.commit()
    elif blocker == "unsafe":
        read.merge_protections[repository.casefold(), "main"] = MergeProtection(
            False, False, True, "unsafe"
        )
    elif blocker == "head":
        writes.pull_requests[repository, 1] = replace(
            writes.pull_requests[repository, 1], head_sha="e" * 40
        )
    monitor = ReleaseMonitor(case.artifact_store, validator, read, writes)
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await monitor(poll, work)
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
        queued = await work.commands.get_by_idempotency_key(f"{run.id}:remote-remediation:1")
        if blocker in {"exhausted", "unsafe", "head"}:
            assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
            assert queued is None
            assert run.remote_remediation_count == (
                policy.remote_remediation_limit if blocker == "exhausted" else 0
            )
            return
        assert run.state is RunState.REMEDIATING
        assert run.remote_remediation_count == 1
        assert queued.command_type == "update_base"
        assert queued.payload["target_base_sha"] == target
        assert queued.expected_run_version == run.version
        assert not writes.pull_requests[repository, 1].merged

    from forge.application.services.base_update import BaseUpdateService
    from forge.application.services.recovery import OperationExecutor
    from forge.persistence.repositories.commands import PostgresCommandRepository
    from forge.persistence.repositories.operations import PostgresOperationRepository
    from forge.release.fake_github_write import FakeGitHubWriteCrash

    commands = PostgresCommandRepository(factory)
    await commands.complete(poll.id, worker_id=poll.lease_owner)
    command = await commands.claim_next(worker_id="base-updater", lease_seconds=120)
    calls = []
    next_head = "e" * 40
    original_head = git.head
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    now = [datetime.now(UTC)]
    async with PostgresUnitOfWork(factory) as work:
        deadline = await work.runs.duration_deadline(case.run_id)

    async def update(repository, number, expected_head):
        calls.append(expected_head)
        updated = replace(writes.pull_requests[repository, number], head_sha=next_head)
        writes.pull_requests[repository, number] = updated
        writes.branch_shas[repository, updated.head_ref] = updated.head_sha
        if blocker in {"uncertain", "uncertain_deadline"}:
            from forge.release.github_write import GitHubWriteError

            if blocker == "uncertain_deadline":
                now[0] = deadline + timedelta(seconds=1)
            raise GitHubWriteError("uncertain")
        if blocker == "adoption_uncertain":
            return updated
        raise FakeGitHubWriteCrash()

    writes.update_branch = update

    class Adoption:
        count = 0

        async def adopt(self, tree, policy, previous, head, base):
            self.count += 1
            git.head = head
            if blocker == "adoption_uncertain":
                from forge.release.git_adoption import ManagedAdoptionError

                raise ManagedAdoptionError()
            raise RuntimeError("local adoption crash")

        async def inspect(self, tree, policy, previous, head, base):
            assert git.head == head

    adoption = Adoption()
    service = BaseUpdateService(
        case.artifact_store,
        validator,
        read,
        writes,
        lambda policy: adoption,
        OperationExecutor(PostgresOperationRepository(factory), execution_lease_seconds=1),
        clock=SimpleNamespace(now=lambda: now[0]),
    )
    if blocker in {"evidence_invalid", "expired_before"}:
        if blocker == "evidence_invalid":
            git.head = "d" * 40
        else:
            now[0] = deadline + timedelta(seconds=1)
        for _ in range(2):
            async with PostgresUnitOfWork(factory) as work:
                await service.execute(command, work)
        async with PostgresUnitOfWork(factory) as work:
            run = await work.runs.get(case.run_id)
            assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
            assert (
                await work.auth.get_approval(approval_id=approval_id)
            ).invalidated_at is not None
            events = [
                e
                for e in await work.events.list_after(run.id, 0)
                if e.event_type == "run.base_update_intervention"
            ]
            assert len(events) == 1
            assert events[0].payload["reason"] == (
                "base_update_duration_exhausted"
                if blocker == "expired_before"
                else "base_update_evidence_invalid"
            )
        assert calls == [] and adoption.count == 0
        return
    if blocker in {"uncertain", "uncertain_deadline", "adoption_uncertain"}:
        from forge.domain.operation import OperationStatus

        for _ in range(2):
            async with PostgresUnitOfWork(factory) as work:
                await service.execute(command, work)
        async with PostgresUnitOfWork(factory) as work:
            run = await work.runs.get(case.run_id)
            assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
            events = await work.events.list_after(run.id, 0)
            intervention = [e for e in events if e.event_type == "run.base_update_intervention"]
            assert len(intervention) == 1
            intent = await work.operations.get_by_idempotency_key(
                intervention[0].payload["operation_key"]
            )
            assert intent.status is OperationStatus.NEEDS_RECONCILIATION
            assert intent.kind == (
                "adopt_base" if blocker == "adoption_uncertain" else "update_branch"
            )
            assert (
                await work.auth.get_approval(approval_id=approval_id)
            ).invalidated_at is not None
        assert calls == [original_head]
        assert adoption.count == int(blocker == "adoption_uncertain")
        return
    from forge.application.ports.commands import CommandRecoveryRequired
    from forge.persistence.models.execution import RunCommand

    original_open = case.artifact_store.open_bytes
    for malformed in (b"{", b"[]"):

        async def corrupt_observation(digest, *, max_bytes=None, malformed=malformed):
            if digest == command.payload["observation_digest"]:
                return malformed
            return await original_open(digest, max_bytes=max_bytes)

        with monkeypatch.context() as patch:
            patch.setattr(case.artifact_store, "open_bytes", corrupt_observation)
            async with PostgresUnitOfWork(factory) as work:
                with pytest.raises(CommandRecoveryRequired, match="observation"):
                    await service.execute(command, work)
                assert calls == [] and adoption.count == 0

    # An observation alone cannot authorize effects before its monitor command settles.
    async with PostgresUnitOfWork(factory) as work:
        source = await work.session.get(RunCommand, poll.id)
        source.status = "LEASED"
        source.lease_owner = poll.lease_owner
        source.lease_expires_at = poll.lease_expires_at
        source.completed_at = None
        await work.session.flush()
        with pytest.raises(CommandRecoveryRequired, match="source"):
            await service.execute(command, work)
        assert calls == [] and adoption.count == 0
    if blocker in {"paused_before", "paused_repeated"}:
        from test_release_publication_resume import resumed_release

        command = await resumed_release(case, command, factory)
        if blocker == "paused_repeated":
            command = await resumed_release(case, command, factory)
    for failure in (FakeGitHubWriteCrash, RuntimeError):
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(failure):
                await service.execute(command, work)
        if blocker and blocker.startswith("startup"):
            from forge.application.services.recovery import RecoveryError, RecoveryService
            from forge.worker.base_recovery import base_recovery_adapters

            adapters = base_recovery_adapters(
                factory, case.artifact_store, validator, read, writes, lambda _: adoption
            )
            operations = PostgresOperationRepository(factory)
            pending = (await operations.list_unresolved())[0]
            with pytest.raises(RecoveryError, match="cannot invoke"):
                await adapters[pending.kind].invoke(pending)
            if blocker == "startup_bad_evidence":
                original = case.artifact_store.open_bytes

                async def corrupted(digest, *, original=original, **kwargs):
                    if digest == command.payload["observation_digest"]:
                        return b"{}"
                    return await original(digest, **kwargs)

                monkeypatch.setattr(case.artifact_store, "open_bytes", corrupted)
                with pytest.raises(RecoveryError, match="unresolved"):
                    await RecoveryService(operations).reconcile_all(adapters)
                assert len(calls) == 1 and adoption.count == 0
                return
            if blocker == "startup_paused" and failure is RuntimeError:
                from forge.application.handlers.run_controls import PauseRunHandler
                from forge.application.ports.commands import CommandLane

                async with PostgresUnitOfWork(factory) as work:
                    run = await work.runs.get(case.run_id)
                    await work.commands.enqueue(
                        run_id=run.id, command_type="pause", idempotency_key=f"{run.id}:pause-base",
                        payload={}, expected_run_version=run.version, actor_id=command.actor_id,
                    )
                    await work.commit()
                pause = await commands.claim_next(worker_id="pause-base", lease_seconds=120, lane=CommandLane.CONTROL)
                async with PostgresUnitOfWork(factory) as work:
                    await PauseRunHandler()(pause, work)
                await commands.complete(pause.id, worker_id=pause.lease_owner)
            recovered = await RecoveryService(operations).reconcile_all(adapters)
            assert len(recovered) == 1
            assert len(calls) == 1
            assert adoption.count == int(failure is RuntimeError)
            if blocker == "startup_paused" and failure is RuntimeError:
                async with PostgresUnitOfWork(factory) as work:
                    assert (await work.runs.get(case.run_id)).state is RunState.PAUSED
                return
    if blocker == "paused_after":
        from forge.application.services.recovery import RecoveryService
        from forge.worker.base_recovery import base_recovery_adapters
        from test_release_publication_resume import resumed_release

        await RecoveryService(PostgresOperationRepository(factory)).reconcile_all(
            base_recovery_adapters(factory, case.artifact_store, validator, read, writes, lambda _: adoption)
        )
        command = await resumed_release(case, command, factory)
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await service.execute(command, work)
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
        record = await work.releases.get_for_run(run.id)
        assert run.state is RunState.VALIDATING
        assert run.base_sha == git.worktree.base_sha
        assert record.pull_request.head_sha == "e" * 40
        assert record.pull_request.base_sha == target
        from forge.persistence.models import PullRequest

        projection = await work.session.get(PullRequest, record.id)
        assert projection.checks == {} and projection.review_state == {}
        assert projection.merge_state is None
        assert record.base_update_intent_id and record.base_adoption_intent_id
        events = await work.events.list_after(run.id, 0)
        settled = [e for e in events if e.event_type == "run.base_updated"]
        assert len(settled) == 1
        validation = await work.commands.get_by_idempotency_key(
            settled[0].payload["validation_key"]
        )
        assert (
            validation.command_type == "validate" and validation.expected_run_version == run.version
        )
        assert len(calls) == 1 and adoption.count == 1
    if blocker == "startup":
        return
    if blocker in {"replay_receipt", "replay_command", "replay_schema"}:
        async with PostgresUnitOfWork(factory) as work:
            if blocker in {"replay_receipt", "replay_schema"}:
                from forge.persistence.models import OperationIntent

                row = await work.session.get(OperationIntent, record.base_adoption_intent_id)
                if blocker == "replay_receipt":
                    row.outcome_payload = {}
                else:
                    row.outcome_schema_version = 2
            else:
                row = await work.session.get(RunCommand, validation.id)
                row.payload = {"semantic_attempt": 999}
            await work.commit()
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(CommandRecoveryRequired):
                await service.execute(command, work)
        assert len(calls) == 1 and adoption.count == 1
        return
    if blocker and blocker.startswith("paused_settled"):
        from forge.domain.command import CommandStatus
        from test_release_publication_resume import resumed_release

        if blocker != "paused_settled":
            if blocker == "paused_settled_receipt":
                from forge.persistence.models import OperationIntent

                async with PostgresUnitOfWork(factory) as work:
                    row = await work.session.get(OperationIntent, record.base_adoption_intent_id)
                    row.outcome_payload = {}
                    await work.commit()
            with pytest.raises(CommandRecoveryRequired):
                await resumed_release(
                    case, command, factory, continued_type="validate",
                    renewed=blocker == "paused_settled_renewed",
                )
            async with PostgresUnitOfWork(factory) as work:
                assert (await work.commands.get(command.id)).status is CommandStatus.LEASED
                assert (await work.runs.get(case.run_id)).state is RunState.PAUSED
            assert len(calls) == 1 and adoption.count == 1
            return
        continued = await resumed_release(case, command, factory, continued_type="validate")
        async with PostgresUnitOfWork(factory) as work:
            assert (await work.commands.get(command.id)).status is CommandStatus.COMPLETED
            assert continued.payload["semantic_attempt"] == validation.payload["semantic_attempt"]
        assert len(calls) == 1 and adoption.count == 1
        await _review_adopted_candidate(
            case, git, factory, commands, validation_command=continued, pause_review=True
        )
        return
    await commands.complete(command.id, worker_id=command.lease_owner)
    await _review_adopted_candidate(case, git, factory, commands)
    async with PostgresUnitOfWork(factory) as work:
        verified = await validator.validate_published(work, case.run_id, approval_id)
        assert verified.evidence.base_sha == git.worktree.base_sha
        assert verified.remote_base_sha == target
        assert verified.candidate_head == "e" * 40
        approval = await work.auth.get_approval(approval_id=approval_id)
        from forge.domain.approval import canonical_digest

        assert canonical_digest(verified.evidence) != approval.evidence_digest
    from datetime import UTC, datetime, timedelta

    from forge.domain.approval import MergeApprovalEvidence
    from forge.domain.github import CheckSnapshot

    async with PostgresUnitOfWork(factory) as work:
        next_poll = await work.commands.get_by_idempotency_key(f"{case.run_id}:monitor-pr:2")
        row = await work.session.get(RunCommand, next_poll.id)
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()
    next_poll = await commands.claim_next(worker_id="adopted-monitor", lease_seconds=120)
    # A second advance must consume a new slot and supersede the first review lineage.
    first_digest = canonical_digest(verified.evidence)
    first_receipts = (record.base_update_intent_id, record.base_adoption_intent_id)
    target, next_head = "d" * 40, "c" * 40
    read.bases[repository.casefold(), "main"] = target
    writes.branch_shas[repository, "main"] = target
    writes.pull_requests[repository, 1] = replace(
        writes.pull_requests[repository, 1], base_sha=target
    )
    async with PostgresUnitOfWork(factory) as work:
        await monitor(next_poll, work)
        assert (await work.runs.get(case.run_id)).state is RunState.REMEDIATING
    await commands.complete(next_poll.id, worker_id=next_poll.lease_owner)
    command = await commands.claim_next(worker_id="second-base", lease_seconds=120)
    assert command.command_type == "update_base" and command.payload["remote_attempt"] == 2
    for failure in (FakeGitHubWriteCrash, RuntimeError):
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(failure):
                await service.execute(command, work)
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await service.execute(command, work)
    await commands.complete(command.id, worker_id=command.lease_owner)
    await _review_adopted_candidate(
        case, git, factory, commands, repair=repair_kind, writes=writes, validator=validator
    )
    next_head = git.head
    async with PostgresUnitOfWork(factory) as work:
        verified = await validator.validate_published(work, case.run_id, approval_id)
        assert canonical_digest(verified.evidence) != first_digest
        assert verified.evidence.base_sha == git.worktree.base_sha
        assert verified.remote_base_sha == target and verified.candidate_head == next_head
        assert (await work.runs.get(case.run_id)).remote_remediation_count == 2
        next_poll = await work.commands.get_by_idempotency_key(f"{case.run_id}:monitor-pr:3")
        row = await work.session.get(RunCommand, next_poll.id)
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()
    assert len(calls) == 2 and adoption.count == 2
    next_poll = await commands.claim_next(worker_id="second-adopted-monitor", lease_seconds=120)
    read.checks[repository.casefold(), git.head] = (
        CheckSnapshot("ci", "completed", "success", head_sha=git.head),
    )
    async with PostgresUnitOfWork(factory) as work:
        await monitor(next_poll, work)
        run = await work.runs.get(case.run_id)
        assert run.state is RunState.AWAITING_MERGE_APPROVAL
        gate = MergeApprovalEvidence.model_validate_json(
            await case.artifact_store.open_bytes(run.pending_evidence_digest)
        )
        assert gate.base_sha == target and gate.head_sha == git.head
        assert gate.validation_digest == verified.evidence.validation_digest
        assert gate.review_digest == verified.evidence.review_digest
    from forge.application.services.merge_evidence import MergeEvidenceValidator
    from forge.persistence.models import PullRequest
    from forge.release.merge import MergeController, StaleMergeEvidence

    merge_evidence = MergeEvidenceValidator(
        case.artifact_store, validator, MergeController(read, writes)
    )
    async with PostgresUnitOfWork(factory) as work:
        assert await merge_evidence.validate(work, case.run_id) == gate
    # A prior update's valid receipts cannot authorize the newer candidate.
    async with PostgresUnitOfWork(factory) as work:
        row = await work.session.get(PullRequest, record.id)
        row.base_update_intent_id, row.base_adoption_intent_id = first_receipts
        await work.session.flush()
        with pytest.raises(StaleMergeEvidence):
            await merge_evidence.validate(work, case.run_id)
    for drift in ("head", "base"):
        prior_head = git.head
        if drift == "head":
            git.head = "9" * 40
        else:
            read.bases[repository.casefold(), "main"] = git.worktree.base_sha
        try:
            async with PostgresUnitOfWork(factory) as work:
                with pytest.raises(StaleMergeEvidence):
                    await merge_evidence.validate(work, case.run_id)
        finally:
            git.head = prior_head
            read.bases[repository.casefold(), "main"] = target


async def _review_adopted_candidate(
    case, git, factory, commands, *, repair=False, expect_push=False, writes=None, validator=None,
    validation_command=None, pause_review=False, check_repair_lineage=True,
):
    from pathlib import Path

    from forge.agents.prompt_loader import PromptLoader
    from forge.application.services.approved_plan import ApprovedPlanLoader
    from forge.application.services.delivery import DeliveryService
    from forge.application.services.recovery import OperationExecutor
    from forge.application.services.review import ReviewService
    from forge.application.services.review_decision import ReviewDecisionService
    from forge.application.services.validation import ValidationService
    from forge.domain.run import RunState
    from forge.persistence.repositories.operations import PostgresOperationRepository
    from test_delivery_development import _Reader
    from test_delivery_review import _Gateway as ReviewGateway
    from test_delivery_validation import _CheckingRunner

    command = validation_command or await commands.claim_next(worker_id="base-validation", lease_seconds=120)
    assert command.command_type == "validate"

    async def environment(_run, _policy, _worktree):
        return {}

    class Runner(_CheckingRunner):
        async def run_terminal(self, request):
            from dataclasses import replace

            terminal = await super().run_terminal(request)
            if repair == "validation":
                return replace(terminal, result=replace(terminal.result, exit_code=1))
            return terminal

    validation = ValidationService(
        case.artifact_store,
        uow_factory=lambda: PostgresUnitOfWork(factory),
        operation_executor=OperationExecutor(PostgresOperationRepository(factory)),
        git_factory=lambda _: git,
        runner_factory=Runner(factory, case.run_id, case.artifact_store),
        environment_resolver=environment,
    )
    async with PostgresUnitOfWork(factory) as work:
        await DeliveryService(
            case.artifact_store, validation=validation, git_factory=lambda _: git
        ).validate(command, work)
    await commands.complete(command.id, worker_id=command.lease_owner)
    if repair == "validation":
        return await _repair_adopted_candidate(case, git, factory, commands, writes, validator)
    review = await commands.claim_next(worker_id="base-review", lease_seconds=120)
    assert review.command_type == "review"
    if pause_review:
        from test_release_publication_resume import resumed_release

        review = await resumed_release(case, review, factory)

    class Gateway(ReviewGateway):
        async def execute(self, request):
            result = await super().execute(request)
            if repair == "review":
                return result.model_copy(
                    update={
                        "output": result.output.model_copy(
                            update={
                                "decision": "request_changes",
                                "missing_evidence": ("post-adoption regression",),
                            }
                        )
                    }
                )
            return result

    reviewer = ReviewService(
        Gateway(factory),
        case.artifact_store,
        PromptLoader(Path(__file__).resolve().parents[2] / "agents"),
        ApprovedPlanLoader(case.artifact_store),
        lambda _: git,
        lambda _p, _w: _Reader(),
    )
    async with PostgresUnitOfWork(factory) as work:
        await reviewer.execute(review, work)
    decision = ReviewDecisionService(case.artifact_store, git_factory=lambda _: git)
    if expect_push and check_repair_lineage:
        from uuid import UUID

        from forge.application.ports.commands import CommandRecoveryRequired
        from forge.persistence.models import RunCommand

        async with PostgresUnitOfWork(factory) as work:
            repairs = [
                e
                for e in await work.events.list_after(case.run_id, 0)
                if e.event_type == "run.remediation_completed"
            ]
            row = await work.session.get(RunCommand, UUID(repairs[-1].payload["command_id"]))
            row.status, row.completed_at = "PENDING", None
            await work.session.flush()
            with pytest.raises(CommandRecoveryRequired, match="lineage"):
                await decision.decide(review, work)
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            result = await decision.decide(review, work)
            if repair:
                assert result.state is RunState.REMEDIATING
                continue
            assert result.state is RunState.MONITORING_PR
            push = await work.commands.get_by_idempotency_key(
                f"{case.run_id}:push-reviewed:{result.version}"
            )
            assert (push is not None) == expect_push
    await commands.complete(review.id, worker_id=review.lease_owner)
    if repair:
        return await _repair_adopted_candidate(case, git, factory, commands, writes, validator)
    if expect_push:
        from dataclasses import replace

        from forge.application.services.recovery import RecoveryService
        from forge.application.services.release import ReleaseService
        from forge.release.fake_github_write import FakeGitHubWriteCrash
        from forge.worker.publication_recovery import publication_recovery_adapters

        calls = []

        class Push:
            async def push(self, tree, policy, head):
                calls.append(head)
                pull = writes.pull_requests[policy.github_repository, 1]
                writes.branch_shas[policy.github_repository, pull.head_ref] = head
                writes.pull_requests[policy.github_repository, 1] = replace(pull, head_sha=head)
                raise FakeGitHubWriteCrash()

        push = await commands.claim_next(worker_id="base-repair-push", lease_seconds=120)
        assert push.command_type == "push_reviewed_pr"
        release = ReleaseService(
            validator,
            writes,
            lambda _: Push(),
            OperationExecutor(PostgresOperationRepository(factory)),
        )
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(FakeGitHubWriteCrash):
                await release.push_reviewed(push, work)
        recovered = await RecoveryService(PostgresOperationRepository(factory)).reconcile_all(
            publication_recovery_adapters(factory, validator, writes)
        )
        assert len(recovered) == 1
        for _ in range(2):
            async with PostgresUnitOfWork(factory) as work:
                await release.push_reviewed(push, work)
        assert calls == [git.head]
        await commands.complete(push.id, worker_id=push.lease_owner)


async def _repair_adopted_candidate(case, git, factory, commands, writes, validator):
    import hashlib
    from pathlib import Path

    from forge.agents.prompt_loader import PromptLoader
    from forge.application.services.approved_plan import ApprovedPlanLoader
    from forge.application.services.development import DevelopmentService
    from test_delivery_development import _Gateway as DeveloperGateway
    from test_delivery_development import _Reader

    class Developer(DeveloperGateway):
        async def execute(self, request):
            result = await super().execute(request)
            git.head = "8" * 40
            candidate = git.candidate_diff(git.worktree)
            return result.model_copy(
                update={
                    "output": result.output.model_copy(
                        update={
                            "local_commit_sha": git.head,
                            "diff_digest": hashlib.sha256(candidate.diff.text.encode()).hexdigest(),
                            "changed_paths": candidate.changed_paths,
                        }
                    )
                }
            )

    remediation = await commands.claim_next(worker_id="base-repair", lease_seconds=120)
    assert remediation.command_type == "remediate"
    developer = DevelopmentService(
        Developer(factory),
        case.artifact_store,
        PromptLoader(Path(__file__).resolve().parents[2] / "agents"),
        ApprovedPlanLoader(case.artifact_store),
        lambda _: git,
        lambda _p, _w: _Reader(),
    )
    async with PostgresUnitOfWork(factory) as work:
        await developer.execute(remediation, work)
    await commands.complete(remediation.id, worker_id=remediation.lease_owner)
    return await _review_adopted_candidate(
        case, git, factory, commands, expect_push=True, writes=writes, validator=validator
    )
