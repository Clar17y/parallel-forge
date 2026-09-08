"""Durable release observations drive polling and merge approval."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.pr_evidence import PrEvidenceValidator
from forge.application.services.projections import ProjectionService
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release import ReleaseService
from forge.application.services.release_monitor import ReleaseMonitor
from forge.domain.github import CheckSnapshot, MergeProtection, ReviewSnapshot
from forge.domain.run import RunState
from forge.persistence.models import RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.release.fake_github_write import FakeGitHubWrite
from test_pr_approval import authorized
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def published(tmp_path, factory):
    case, git, read, approve, approval_command, approval_id = await authorized(tmp_path, factory)
    async with PostgresUnitOfWork(factory) as work:
        await approve(approval_command, work)
    commands = PostgresCommandRepository(factory)
    await commands.complete(approval_command.id, worker_id=approval_command.lease_owner)
    command = await commands.claim_next(worker_id="publisher", lease_seconds=120)
    writes = FakeGitHubWrite()
    async with PostgresUnitOfWork(factory) as work:
        policy = (await ApprovedPlanLoader(case.artifact_store).load(work, case.run_id)).policy
    writes.branch_shas[policy.github_repository, "main"] = git.worktree.base_sha

    class Push:
        async def push(self, worktree, policy, approved_sha):
            writes.branch_shas[policy.github_repository, worktree.identity.branch] = approved_sha

    validator = PrEvidenceValidator(
        case.artifact_store, ApprovedPlanLoader(case.artifact_store), lambda _: git, read
    )
    service = ReleaseService(
        validator,
        writes,
        lambda _: Push(),
        OperationExecutor(PostgresOperationRepository(factory), execution_lease_seconds=1),
    )
    async with PostgresUnitOfWork(factory) as work:
        await service.publish(command, work)
    await commands.complete(command.id, worker_id=command.lease_owner)
    async with PostgresUnitOfWork(factory) as work:
        queued = await work.commands.get_by_idempotency_key(f"{case.run_id}:monitor-pr:1")
        row = await work.session.get(RunCommand, queued.id)
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()
    monitor_command = await commands.claim_next(worker_id="monitor", lease_seconds=120)
    return case, git, read, writes, validator, monitor_command, policy, approval_id


@pytest.mark.parametrize(
    "observation",
    [
        "ready",
        "queue_ready",
        "queue_wrong_method",
        "queue_unknown_method",
        "pending",
        "head_drift",
        "failure",
        "read_timeout",
        "read_permission",
        "read_malformed",
        "read_checks",
        "read_validation",
    ],
)
async def test_monitor_persists_observation_and_replays_without_duplicate_delivery(
    tmp_path, workflow_session_factory, observation
):
    factory = workflow_session_factory
    case, git, read, writes, validator, command, policy, _ = await published(tmp_path, factory)
    repository = policy.github_repository
    read.merge_protections[repository.casefold(), "main"] = MergeProtection(
        True, False, False, "classic", required_check_names=("ci",)
    )
    if observation.startswith("queue_"):
        read.merge_protections[repository.casefold(), "main"] = MergeProtection(
            False,
            True,
            False,
            "queue",
            required_check_names=("ci",),
            merge_queue_method={
                "queue_ready": "squash",
                "queue_wrong_method": "rebase",
                "queue_unknown_method": None,
            }[observation],
        )
    if observation != "pending":
        read.checks[repository.casefold(), git.head] = (
            CheckSnapshot(
                "ci",
                "completed",
                "failure" if observation == "failure" else "success",
                head_sha=git.head,
                summary="Résumé — checks passed ✓",
            ),
        )
    if observation == "head_drift":
        writes.pull_requests[repository, 1] = replace(
            writes.pull_requests[repository, 1], head_sha="f" * 40
        )
    if observation == "ready":
        read.reviews[repository.casefold(), 1] = (
            ReviewSnapshot(
                "reviewer",
                "COMMENTED",
                datetime.now(UTC),
                comment_count=1,
                body="<script>remote text remains evidence</script>",
                feedback=("Check naming",),
            ),
        )
    monitor = ReleaseMonitor(case.artifact_store, validator, read, writes)
    if observation.startswith("read_"):
        from forge.release.github_client import GitHubClientError
        from forge.release.github_write import GitHubWriteError

        base_reads = 0
        original_base = read.get_base

        async def base(*args):
            nonlocal base_reads
            base_reads += 1
            if base_reads > 1:
                raise GitHubClientError("unavailable")
            return await original_base(*args)

        async def unavailable(*args):
            if observation == "read_checks":
                raise GitHubClientError("rate_limited")
            if observation == "read_malformed":
                raise GitHubWriteError("malformed_response")
            raise GitHubWriteError("unavailable" if observation == "read_timeout" else "permission")

        if observation == "read_validation":
            read.get_base = base
        elif observation == "read_checks":
            read.get_checks = unavailable
        else:
            writes.get_pull_request = unavailable
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await monitor(command, work)
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
        assert (
            run.state
            is {
                "ready": RunState.AWAITING_MERGE_APPROVAL,
                "queue_ready": RunState.AWAITING_MERGE_APPROVAL,
                "queue_wrong_method": RunState.AWAITING_HUMAN_INTERVENTION,
                "queue_unknown_method": RunState.AWAITING_HUMAN_INTERVENTION,
                "pending": RunState.MONITORING_PR,
                "head_drift": RunState.AWAITING_HUMAN_INTERVENTION,
                "failure": RunState.REMEDIATING,
                "read_timeout": RunState.MONITORING_PR,
                "read_permission": RunState.AWAITING_HUMAN_INTERVENTION,
                "read_malformed": RunState.AWAITING_HUMAN_INTERVENTION,
                "read_checks": RunState.MONITORING_PR,
                "read_validation": RunState.MONITORING_PR,
            }[observation]
        )
        events = [
            e for e in await work.events.list_after(run.id, 0) if e.event_type == "run.pr_observed"
        ]
        assert len(events) == 1
        descriptor = await work.artifacts.get_by_digest(
            events[0].payload["observation_digest"], run_id=run.id
        )
        assert await case.artifact_store.verify(descriptor.digest)
        import json

        from forge.persistence.models import PullRequest
        from sqlalchemy import select

        wire = json.loads(await case.artifact_store.open_bytes(descriptor.digest))
        if observation == "queue_ready":
            approved = json.loads(await case.artifact_store.open_bytes(run.pending_evidence_digest))
            assert approved["merge_method"] == wire["protection"]["merge_queue_method"] == "squash"
        projected = await work.session.scalar(
            select(PullRequest).where(PullRequest.run_id == run.id)
        )
        assert projected.checks == {
            "observation_digest": descriptor.digest,
            "head_sha": wire.get("pull_request", {}).get("head_sha"),
            "items": wire.get("checks", []),
        }
        assert projected.review_state == {
            "observation_digest": descriptor.digest,
            "head_sha": wire.get("pull_request", {}).get("head_sha"),
            "items": wire.get("reviews", []),
        }
        assert projected.merge_state == events[0].payload["disposition"]
        from forge.persistence.queries.dashboard import DashboardQuery

        cockpit = await ProjectionService(DashboardQuery(factory)).run_projection(
            run.id,
            AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4()),
        )
        from forge.api.schemas.projections import RunProjection

        RunProjection.model_validate(cockpit)
        from forge.persistence.queries.dashboard_lists import DashboardListQuery

        history, truncated = await DashboardListQuery(factory).check_history(run.id, 0, 100)
        assert not truncated
        by_id = {check["id"]: check for check in history}
        for check in cockpit["checks"]:
            assert by_id[check["id"]]["head_sha"] == check["head_sha"]
            assert (
                by_id[check["id"]]["evidence_digest"]
                == cockpit["candidate"]["validation_evidence_digest"]
            )
            assert by_id[check["id"]]["attempt"] is not None
        remote = cockpit["remote_observation"]
        assert remote["observation_digest"] == descriptor.digest
        assert remote["head_sha"] == wire.get("pull_request", {}).get("head_sha")
        assert [item["name"] for item in remote["checks"]] == [
            item["name"] for item in wire.get("checks", [])
        ]
        if observation == "ready":
            from fastapi import FastAPI
            from forge.api.dependencies import require_operator
            from forge.api.routes.artifacts import router_for
            from forge.application.services.artifact_reads import ArtifactReadService
            from forge.persistence.queries.artifacts import PostgresArtifactReadQuery
            from httpx import ASGITransport, AsyncClient

            app = FastAPI()
            app.state.artifact_read_service = ArtifactReadService(
                PostgresArtifactReadQuery(factory), case.artifact_store
            )
            app.dependency_overrides[require_operator] = lambda: object()
            app.include_router(router_for(), prefix="/api")
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                protection_response = await client.get(
                    f"/api/artifacts/{descriptor.digest}/merge-protection"
                )
            assert protection_response.status_code == 200, protection_response.text
            protection_view = protection_response.json()
            merge_wire = json.loads(
                await case.artifact_store.open_bytes(run.pending_evidence_digest)
            )
            assert protection_view["protection_digest"] == merge_wire["protection_digest"]
            assert protection_view["head_sha"] == merge_wire["head_sha"]
            assert protection_view["observed_base_sha"] == merge_wire["base_sha"]
            assert (
                protection_view["protection"]["evidence_source"]
                == wire["protection"]["evidence_source"]
            )
            assert remote["reviews"][0]["comment_count"] == 1
            assert remote["reviews"][0]["requested_changes"] is False
            assert remote["reviews"][0]["body"] == "<script>remote text remains evidence</script>"
        if observation in {"ready", "failure"}:
            assert remote["checks"][0]["summary"] == wire["checks"][0]["summary"]

        next_poll = await work.commands.get_by_idempotency_key(f"{run.id}:monitor-pr:2")
        assert (next_poll is not None) == (
            observation in {"pending", "read_timeout", "read_checks", "read_validation"}
        )
        if next_poll:
            assert next_poll.available_at >= events[0].occurred_at + timedelta(seconds=15)
        if observation == "failure":
            assert run.remote_remediation_count == 1
            delivery = await work.commands.get_by_idempotency_key(f"{run.id}:remote-remediation:1")
            assert delivery.command_type == "remediate_remote"
            assert delivery.payload["observation_digest"] == descriptor.digest


@pytest.mark.parametrize("read_failure", [False, True])
async def test_unchanged_polls_back_off_to_two_minutes_and_stop_at_duration_budget(
    tmp_path, workflow_session_factory, read_failure
):
    factory = workflow_session_factory
    case, _, read, _, validator, command, policy, _ = await published(tmp_path, factory)
    # Reuse the writer from the published setup, with a controllable controller clock.
    async with PostgresUnitOfWork(factory) as work:
        record = await work.releases.get_for_run(case.run_id)
    writes = FakeGitHubWrite()
    writes.pull_requests[policy.github_repository, 1] = record.pull_request
    if read_failure:
        from forge.release.github_write import GitHubWriteError

        async def unavailable(*args):
            raise GitHubWriteError("unavailable")

        writes.get_pull_request = unavailable
    read.merge_protections[policy.github_repository.casefold(), "main"] = MergeProtection(
        True, False, False, "classic", required_check_names=("ci",)
    )

    class Clock:
        value = datetime.now(UTC)

        def now(self):
            return self.value

    clock = Clock()
    monitor = ReleaseMonitor(case.artifact_store, validator, read, writes, clock=clock)
    commands = PostgresCommandRepository(factory)
    for poll, expected_delay in enumerate((15, 30, 60, 120, 120), 1):
        async with PostgresUnitOfWork(factory) as work:
            await monitor(command, work)
            events = [
                e
                for e in await work.events.list_after(case.run_id, 0)
                if e.event_type == "run.pr_observed"
            ]
            assert events[-1].payload["delay_seconds"] == expected_delay
            if poll == 2:
                from uuid import UUID

                from forge.persistence.repositories.release import ReleaseRecordConflict

                old_digest = events[0].payload["observation_digest"]
                with pytest.raises(ReleaseRecordConflict):
                    await work.releases.record_observation(
                        case.run_id,
                        UUID(events[0].payload["source_command_id"]),
                        old_digest,
                        await case.artifact_store.open_bytes(old_digest),
                    )
        await commands.complete(command.id, worker_id=command.lease_owner)
        async with PostgresUnitOfWork(factory) as work:
            queued = await work.commands.get_by_idempotency_key(
                f"{case.run_id}:monitor-pr:{poll + 1}"
            )
            clock.value = queued.available_at
            row = await work.session.get(RunCommand, queued.id)
            row.available_at = datetime.now(UTC) - timedelta(seconds=1)
            await work.commit()
        command = await commands.claim_next(worker_id="monitor", lease_seconds=120)
    async with PostgresUnitOfWork(factory) as work:
        clock.value = await work.runs.duration_deadline(case.run_id)
        await monitor(command, work)
        run = await work.runs.get(case.run_id)
        assert run.state is RunState.AWAITING_HUMAN_INTERVENTION


@pytest.mark.parametrize(
    "outcome,fault",
    [
        ("ready", "missing"),
        ("failure", "missing"),
        ("head_drift", "missing"),
        ("ready", "payload"),
        ("failure", "payload"),
        ("head_drift", "payload"),
        ("ready", "artifact"),
        ("failure", "delivery"),
    ],
)
async def test_monitor_replay_requires_matching_transition_receipt(
    tmp_path, workflow_session_factory, monkeypatch, outcome, fault
):
    from forge.application.ports.commands import CommandRecoveryRequired

    factory = workflow_session_factory
    case, git, read, writes, validator, command, policy, _ = await published(tmp_path, factory)
    read.merge_protections[policy.github_repository.casefold(), "main"] = MergeProtection(
        True, False, False, "classic", required_check_names=("ci",)
    )
    read.checks[policy.github_repository.casefold(), git.head] = (
        CheckSnapshot(
            "ci", "completed", "failure" if outcome == "failure" else "success", head_sha=git.head
        ),
    )
    if outcome == "head_drift":
        writes.pull_requests[policy.github_repository, 1] = replace(
            writes.pull_requests[policy.github_repository, 1], head_sha="f" * 40
        )
    monitor = ReleaseMonitor(case.artifact_store, validator, read, writes)
    async with PostgresUnitOfWork(factory) as work:
        await monitor(command, work)
    receipt = {
        "ready": "run.merge_ready",
        "failure": "run.remote_remediation_requested",
        "head_drift": "run.release_intervention",
    }[outcome]
    async with PostgresUnitOfWork(factory) as work:
        original = work.events.list_after

        async def missing_receipt(run_id, sequence):
            events = await original(run_id, sequence)
            if fault == "missing":
                return [event for event in events if event.event_type != receipt]
            return [
                replace(event, payload={"source_command_id": str(command.id)})
                if fault == "payload" and event.event_type == receipt
                else event
                for event in events
            ]

        monkeypatch.setattr(work.events, "list_after", missing_receipt)
        if fault == "artifact":
            events = await original(case.run_id, 0)
            digest = next(
                e.payload["merge_evidence_digest"] for e in events if e.event_type == receipt
            )
            original_verify = case.artifact_store.verify

            async def missing_merge_artifact(value):
                return False if value == digest else await original_verify(value)

            monkeypatch.setattr(case.artifact_store, "verify", missing_merge_artifact)
        if fault == "delivery":
            original_get = work.commands.get_by_idempotency_key

            async def changed_delivery(key):
                queued = await original_get(key)
                return replace(queued, command_type="update_base") if queued else None

            monkeypatch.setattr(work.commands, "get_by_idempotency_key", changed_delivery)
        with pytest.raises(CommandRecoveryRequired, match="replay outcome"):
            await monitor(command, work)
