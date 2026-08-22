from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

import pytest
from forge.application.ports.audit import OperatorAuditRecord
from forge.application.ports.mutations import ApiMutationRecord
from forge.application.ports.projects import (
    ProjectPolicyRecord,
    ProjectRecord,
    RepositoryInspection,
)
from forge.application.ports.tasks import TaskRecord
from forge.application.services.auth import AuthenticatedActor
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.run import RunSnapshot, RunState, SuspensionKind
from forge.persistence.repositories.commands import IdempotencyConflict
from forge.persistence.repositories.mutations import MutationConflict
from pydantic import ValidationError

ACTOR = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())


@dataclass
class FakeMutations:
    receipts: dict[tuple[UUID, str, str], ApiMutationRecord] = field(default_factory=dict)

    async def reserve(
        self,
        *,
        actor_id: UUID,
        action: str,
        scope: str,
        idempotency_key: str,
        request_digest: str,
    ) -> ApiMutationRecord:
        import hashlib

        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        key = (actor_id, action, key_hash)
        existing = self.receipts.get(key)
        if existing is not None:
            if existing.scope != scope or existing.request_digest != request_digest:
                raise MutationConflict("idempotency key was reused for a different request")
            return ApiMutationRecord(
                id=existing.id,
                actor_id=existing.actor_id,
                action=existing.action,
                scope=existing.scope,
                key_hash=existing.key_hash,
                request_digest=existing.request_digest,
                lifecycle_state=existing.lifecycle_state,
                response_status=existing.response_status,
                response_payload=existing.response_payload,
                resource_kind=existing.resource_kind,
                resource_id=existing.resource_id,
                is_replay=True,
            )
        receipt = ApiMutationRecord(
            id=uuid4(),
            actor_id=actor_id,
            action=action,
            scope=scope,
            key_hash=key_hash,
            request_digest=request_digest,
            lifecycle_state="RESERVED",
            response_status=None,
            response_payload=None,
            resource_kind=None,
            resource_id=None,
            is_replay=False,
        )
        self.receipts[key] = receipt
        return receipt

    async def complete(
        self,
        mutation_id: UUID,
        *,
        response_status: int,
        response_payload: Mapping[str, object],
        resource_kind: str | None = None,
        resource_id: UUID | None = None,
    ) -> ApiMutationRecord:
        for key, receipt in self.receipts.items():
            if receipt.id == mutation_id:
                completed = ApiMutationRecord(
                    id=receipt.id,
                    actor_id=receipt.actor_id,
                    action=receipt.action,
                    scope=receipt.scope,
                    key_hash=receipt.key_hash,
                    request_digest=receipt.request_digest,
                    lifecycle_state="COMPLETED",
                    response_status=response_status,
                    response_payload=dict(response_payload),
                    resource_kind=resource_kind,
                    resource_id=resource_id,
                    is_replay=False,
                )
                self.receipts[key] = completed
                return completed
        raise AssertionError("unknown mutation receipt")


@dataclass
class FakeProjects:
    records: dict[UUID, ProjectRecord] = field(default_factory=dict)

    async def get(self, project_id: UUID, *, for_update: bool = False) -> ProjectRecord:
        del for_update
        return self.records[project_id]


@dataclass
class FakeTasks:
    records: dict[UUID, TaskRecord] = field(default_factory=dict)

    async def get(self, task_id: UUID, *, for_update: bool = False) -> TaskRecord:
        del for_update
        return self.records[task_id]


@dataclass
class FakeRuns:
    records: dict[UUID, RunSnapshot] = field(default_factory=dict)

    async def get(self, run_id: UUID) -> RunSnapshot:
        return self.records[run_id]

    async def get_for_update(self, run_id: UUID) -> RunSnapshot:
        return self.records[run_id]

    async def create(self, run: RunSnapshot) -> None:
        self.records[run.id] = run

    async def list(
        self, *, project_id: UUID | None = None, task_id: UUID | None = None
    ) -> list[RunSnapshot]:
        return [
            run
            for run in self.records.values()
            if (project_id is None or run.project_id == project_id)
            and (task_id is None or run.task_id == task_id)
        ]


@dataclass
class FakeEvents:
    records: list[RunEvent] = field(default_factory=list)
    fail: bool = False

    async def append(self, event: RunEvent) -> RunEvent:
        if self.fail:
            raise RuntimeError("injected event failure")
        self.records.append(event)
        return event


@dataclass
class FakeCommands:
    records: dict[str, CommandEnvelope] = field(default_factory=dict)

    async def enqueue(self, **kwargs: object) -> CommandEnvelope:
        key = kwargs["idempotency_key"]
        assert isinstance(key, str)
        existing = self.records.get(key)
        if existing is not None:
            if (
                existing.run_id != kwargs["run_id"]
                or existing.command_type != kwargs["command_type"]
                or existing.expected_run_version != kwargs["expected_run_version"]
                or existing.actor_id != kwargs["actor_id"]
                or dict(existing.payload) != dict(kwargs["payload"])
            ):
                raise IdempotencyConflict("command key was reused for another request")
            return existing
        now = datetime.now(UTC)
        command = CommandEnvelope(
            id=uuid4(),
            run_id=kwargs["run_id"],
            command_type=kwargs["command_type"],
            idempotency_key=key,
            payload=kwargs["payload"],
            status=CommandStatus.PENDING,
            expected_run_version=kwargs.get("expected_run_version", 0),
            actor_id=kwargs.get("actor_id"),
            payload_schema_version=kwargs.get("payload_schema_version", 1),
            attempt=0,
            available_at=now,
            lease_owner=None,
            lease_expires_at=None,
            created_at=now,
        )
        self.records[key] = command
        return command

    async def get_by_idempotency_key(self, idempotency_key: str) -> CommandEnvelope | None:
        return self.records.get(idempotency_key)


@dataclass
class FakeAudit:
    records: list[OperatorAuditRecord] = field(default_factory=list)

    async def append(self, **kwargs: object) -> OperatorAuditRecord:
        record = OperatorAuditRecord(
            id=uuid4(),
            correlation_id=kwargs.pop("correlation_id", uuid4()),
            created_at=datetime.now(UTC),
            schema_version=kwargs.pop("schema_version", 1),
            **kwargs,
        )
        self.records.append(record)
        return record


@dataclass
class FakeUow:
    projects: FakeProjects
    tasks: FakeTasks
    runs: FakeRuns
    events: FakeEvents
    commands: FakeCommands
    mutations: FakeMutations
    audit: FakeAudit
    committed: bool = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.committed = False


class FakeInspector:
    def __init__(self, repository: Path) -> None:
        self.repository = repository

    def inspect(self, **kwargs: str) -> RepositoryInspection:
        del kwargs
        return RepositoryInspection(
            canonical_path=str(self.repository.resolve()),
            github_repository="owner/repo",
            default_branch="main",
            base_ref="refs/heads/main",
            base_sha="b" * 40,
        )


def _project(project_id: UUID) -> ProjectRecord:
    policy = ProjectPolicyRecord(
        project_id=project_id,
        version=3,
        policy_digest="a" * 64,
        document_schema_version=1,
        document={"database": {"enabled": False}},
    )
    return ProjectRecord(
        id=project_id,
        name="Forge",
        canonical_path="C:/repo",
        canonical_path_key="c:/repo",
        github_repository="owner/repo",
        default_branch="main",
        instructions_path=None,
        current_policy_version=3,
        policy=policy,
    )


def _task(task_id: UUID, project_id: UUID) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        project_id=project_id,
        title="Exact title",
        body="private body must not enter event",
        source_url=None,
        source_updated_at=None,
        untrusted_external_content=False,
        normalized_text="Exact title\n\nprivate body must not enter event",
        task_digest="c" * 64,
        external_source=None,
        external_id=None,
    )


def _uow(
    *, state: RunState = RunState.CREATED, branch_name: str | None = None
) -> tuple[FakeUow, UUID, UUID]:
    project_id = uuid4()
    task_id = uuid4()
    work = FakeUow(
        projects=FakeProjects({project_id: _project(project_id)}),
        tasks=FakeTasks({task_id: _task(task_id, project_id)}),
        runs=FakeRuns(),
        events=FakeEvents(),
        commands=FakeCommands(),
        mutations=FakeMutations(),
        audit=FakeAudit(),
    )
    if state is not RunState.CREATED:
        run = RunSnapshot(
            id=uuid4(),
            project_id=project_id,
            task_id=task_id,
            state=state,
            version=4,
            branch_name=branch_name,
            suspended_state=RunState.PLANNING if state is RunState.PAUSED else None,
            suspension_kind=SuspensionKind.PAUSE if state is RunState.PAUSED else None,
        )
        work.runs.records[run.id] = run
    return work, project_id, task_id


@pytest.mark.asyncio
async def test_create_run_snapshots_policy_base_and_enqueues_planning_once(tmp_path: Path) -> None:
    from forge.application.services.runs import RunService

    work, project_id, task_id = _uow()
    service = RunService(
        lambda: work,
        repository_inspector=FakeInspector(tmp_path / "repo"),
        data_root=tmp_path,
    )
    result = await service.create_run(actor=ACTOR, idempotency_key="run-1", task_id=task_id)

    assert result.project_id == project_id
    assert result.policy_version == 3
    assert result.base_ref == "refs/heads/main"
    assert result.base_sha == "b" * 40
    assert result.state is RunState.CREATED
    assert result.version == 0
    assert len(work.events.records) == 1
    assert work.events.records[0].event_type == "run.created"
    assert work.events.records[0].actor_id == ACTOR.actor_id
    assert "body" not in work.events.records[0].payload
    commands = list(work.commands.records.values())
    assert len(commands) == 1
    assert commands[0].command_type == "start_planning"
    assert commands[0].payload == {}
    assert commands[0].expected_run_version == 0
    assert commands[0].actor_id == ACTOR.actor_id
    assert commands[0].idempotency_key == f"{result.id}:start-planning"

    replay = await service.create_run(actor=ACTOR, idempotency_key="run-1", task_id=task_id)
    assert replay.id == result.id
    assert len(work.events.records) == 1
    assert len(work.commands.records) == 1

    with pytest.raises(MutationConflict):
        await service.create_run(actor=ACTOR, idempotency_key="run-1", task_id=uuid4())


@pytest.mark.asyncio
async def test_run_queries_return_safe_snapshots() -> None:
    from forge.application.services.runs import RunService

    work, project_id, task_id = _uow()
    run = RunSnapshot(id=uuid4(), project_id=project_id, task_id=task_id, policy_version=3)
    work.runs.records[run.id] = run
    service = RunService(lambda: work)
    assert await service.get(run.id) == run
    assert await service.list(project_id=project_id) == [run]
    assert (await service.get(run.id)).secret_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_type", "state", "feedback", "delete_branch", "confirm_branch_name"),
    [
        ("pause", RunState.PLANNING, None, False, None),
        ("resume", RunState.PAUSED, None, False, None),
        ("cancel", RunState.AWAITING_HUMAN_INTERVENTION, None, False, None),
        ("request_plan_revision", RunState.AWAITING_PLAN_APPROVAL, "revise", False, None),
        ("request_candidate_changes", RunState.AWAITING_PR_APPROVAL, "fix", False, None),
        ("reject_merge", RunState.AWAITING_MERGE_APPROVAL, "no", False, None),
        ("teardown_run_resources", RunState.COMPLETED, None, True, "forge/main"),
    ],
)
async def test_run_commands_accept_only_the_closed_state_matrix(
    command_type: str,
    state: RunState,
    feedback: str | None,
    delete_branch: bool,
    confirm_branch_name: str | None,
) -> None:
    from forge.application.services.runs import (
        RunCommandRequest,
        RunCommandService,
    )

    work, _, _ = _uow(state=state, branch_name="forge/main")
    run_id = next(iter(work.runs.records))
    service = RunCommandService(lambda: work)
    request = RunCommandRequest(
        command_type=command_type,
        expected_run_version=4,
        feedback=feedback,
        delete_branch=delete_branch,
        confirm_branch_name=confirm_branch_name,
    )
    command = await service.enqueue(
        actor=ACTOR, run_id=run_id, idempotency_key=f"key-{command_type}", request=request
    )
    assert command.command_type == command_type
    assert work.runs.records[run_id].state is state
    assert work.runs.records[run_id].version == 4
    if feedback is not None:
        expected_payload = {"feedback": feedback}
    elif delete_branch:
        expected_payload = {"delete_branch": True, "confirm_branch_name": "forge/main"}
    elif command_type == "teardown_run_resources":
        expected_payload = {"delete_branch": False}
    else:
        expected_payload = {}
    assert dict(command.payload) == expected_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_type", "state"),
    [
        ("pause", RunState.PAUSED),
        ("resume", RunState.PLANNING),
        ("cancel", RunState.COMPLETED),
        ("request_plan_revision", RunState.PLANNING),
        ("request_candidate_changes", RunState.REVIEWING),
        ("reject_merge", RunState.MONITORING_PR),
        ("teardown_run_resources", RunState.PLANNING),
    ],
)
async def test_run_commands_reject_invalid_states(command_type: str, state: RunState) -> None:
    from forge.application.services.runs import (
        RunCommandRequest,
        RunCommandService,
        RunCommandValidationError,
    )

    work, _, _ = _uow(state=state, branch_name="forge/main")
    run_id = next(iter(work.runs.records))
    service = RunCommandService(lambda: work)
    feedback = (
        "feedback"
        if command_type in {"request_plan_revision", "request_candidate_changes", "reject_merge"}
        else None
    )
    request = RunCommandRequest(
        command_type=command_type, expected_run_version=4, feedback=feedback
    )
    with pytest.raises(RunCommandValidationError):
        await service.enqueue(
            actor=ACTOR, run_id=run_id, idempotency_key=f"invalid-{command_type}", request=request
        )


@pytest.mark.asyncio
async def test_run_command_replay_and_changed_request_conflict_without_transition() -> None:
    from forge.application.services.runs import RunCommandRequest, RunCommandService

    work, _, _ = _uow(state=RunState.PLANNING)
    run_id = next(iter(work.runs.records))
    service = RunCommandService(lambda: work)
    request = RunCommandRequest(command_type="pause", expected_run_version=4)
    first = await service.enqueue(
        actor=ACTOR, run_id=run_id, idempotency_key="command-1", request=request
    )
    replay = await service.enqueue(
        actor=ACTOR, run_id=run_id, idempotency_key="command-1", request=request
    )
    assert replay.id == first.id
    with pytest.raises(IdempotencyConflict):
        await service.enqueue(
            actor=ACTOR,
            run_id=run_id,
            idempotency_key="command-1",
            request=RunCommandRequest(command_type="cancel", expected_run_version=4),
        )
    assert work.runs.records[run_id].version == 4


@pytest.mark.asyncio
async def test_run_command_replay_survives_worker_transition() -> None:
    from forge.application.services.runs import RunCommandRequest, RunCommandService

    work, _, _ = _uow(state=RunState.PLANNING)
    run_id = next(iter(work.runs.records))
    service = RunCommandService(lambda: work)
    request = RunCommandRequest(command_type="pause", expected_run_version=4)
    first = await service.enqueue(
        actor=ACTOR, run_id=run_id, idempotency_key="command-after-transition", request=request
    )

    work.runs.records[run_id] = replace(work.runs.records[run_id], state=RunState.PAUSED, version=5)
    replay = await service.enqueue(
        actor=ACTOR, run_id=run_id, idempotency_key="command-after-transition", request=request
    )

    assert replay.id == first.id
    with pytest.raises(IdempotencyConflict):
        await service.enqueue(
            actor=ACTOR,
            run_id=run_id,
            idempotency_key="command-after-transition",
            request=RunCommandRequest(command_type="cancel", expected_run_version=4),
        )


@pytest.mark.asyncio
async def test_run_command_feedback_and_teardown_confirmation_are_closed() -> None:
    from forge.application.services.runs import (
        RunCommandRequest,
        RunCommandService,
        RunCommandValidationError,
    )

    with pytest.raises(ValidationError):
        RunCommandRequest(command_type="request_plan_revision", expected_run_version=0)
    with pytest.raises(ValidationError):
        RunCommandRequest(command_type="pause", expected_run_version=0, feedback="unexpected")
    with pytest.raises(ValidationError):
        RunCommandRequest(command_type="pause", expected_run_version=0, arbitrary="nope")

    work, _, _ = _uow(state=RunState.COMPLETED, branch_name=None)
    run_id = next(iter(work.runs.records))
    service = RunCommandService(lambda: work)
    with pytest.raises(RunCommandValidationError, match="branch"):
        await service.enqueue(
            actor=ACTOR,
            run_id=run_id,
            idempotency_key="teardown-1",
            request=RunCommandRequest(
                command_type="teardown_run_resources",
                expected_run_version=4,
                delete_branch=True,
                confirm_branch_name="forge/main",
            ),
        )
