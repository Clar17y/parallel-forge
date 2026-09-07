from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from forge.application.ports.evidence import (
    EvidenceCorruptLineage,
    EvidenceInputPurpose,
    EvidenceReadScope,
)
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.tools import ControlledToolService
from forge.domain.actor import AgentRole
from forge.domain.evidence import ValidationEvidenceManifest
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState
from forge.domain.tool import ToolCallStatus, ToolName, ToolRequest

from apps.orchestrator.tests.application.test_tool_audit import (
    BASE_SHA,
    PROJECT_ID,
    RUN_ID,
    _context,
    _Git,
    _project_policy,
    _service,
)

_LIVE_HEAD = "f" * 40
_WORKTREE_ID = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, "forge-test", False).worktree_name


class _GitWithHead(_Git):
    def __init__(self, repository: Path, worktree: ManagedWorktree) -> None:
        super().__init__(repository, worktree)
        self.calls = 0

    def head_sha(self, worktree: ManagedWorktree) -> str:
        assert worktree is self._worktree
        self.calls += 1
        return _LIVE_HEAD


class _Reader:
    def __init__(self, manifest: ValidationEvidenceManifest | None = None) -> None:
        self.manifest = manifest
        self.scopes: list[tuple[EvidenceInputPurpose, EvidenceReadScope]] = []
        self.error: Exception | None = None

    async def read(
        self, purpose: EvidenceInputPurpose, scope: EvidenceReadScope
    ) -> ValidationEvidenceManifest | None:
        self.scopes.append((purpose, scope))
        if self.error is not None:
            raise self.error
        return self.manifest


def _review_service(
    tmp_path: Path, reader: _Reader | None
) -> tuple[ControlledToolService, object, _GitWithHead]:
    policy = _project_policy(tmp_path)
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, "forge-test", False)
    worktree = ManagedWorktree(identity=identity, path=tmp_path, base_sha=BASE_SHA)
    git = _GitWithHead(tmp_path, worktree)
    _, work, _ = _service(
        tmp_path,
        controlled_git=git,
        managed_worktree=worktree,
        project_policy=policy,
    )
    work.runs.run = replace(
        work.runs.run,
        state=RunState.REVIEWING,
        branch_name="forge-test",
        base_sha=BASE_SHA,
        worktree_path=str(tmp_path),
    )
    return (
        ControlledToolService(
            lambda: work,
            repository_reader=None,
            controlled_git=git,
            worktree=worktree,
            evidence_reader=reader,  # type: ignore[arg-type]
        ),
        work,
        git,
    )


async def test_evidence_tool_uses_live_head_and_caller_context_scope(tmp_path: Path) -> None:
    manifest = ValidationEvidenceManifest(
        evidence_set_id=RUN_ID,
        run_id=RUN_ID,
        step_id=RUN_ID,
        policy_version=1,
        head_sha=_LIVE_HEAD,
    )
    reader = _Reader(manifest)
    service, _, git = _review_service(tmp_path, reader)
    context = _context(role=AgentRole.REVIEWER, worktree_id=_WORKTREE_ID)

    result = await service.invoke(
        context, ToolRequest(name=ToolName.VALIDATION_RESULTS_READ, arguments={})
    )

    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.metadata["head_sha"] == _LIVE_HEAD
    assert result.artifact_digests
    assert reader.scopes == [
        (
            EvidenceInputPurpose.VALIDATION_RESULTS,
            EvidenceReadScope(RUN_ID, 1, context.agent_execution_id, context.step_id, _LIVE_HEAD),
        )
    ]
    assert git.calls == 1


async def test_absent_prior_is_successful_empty_evidence(tmp_path: Path) -> None:
    reader = _Reader()
    service, _, _ = _review_service(tmp_path, reader)

    result = await service.invoke(
        _context(role=AgentRole.REVIEWER, worktree_id=_WORKTREE_ID),
        ToolRequest(name=ToolName.REVIEW_ARTIFACTS_READ, arguments={}),
    )

    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.metadata == {"evidence": None}
    assert reader.scopes[0][0] is EvidenceInputPurpose.PRIOR_REVIEW


async def test_required_validation_absence_and_corruption_are_audited_failures(
    tmp_path: Path,
) -> None:
    reader = _Reader()
    service, work, _ = _review_service(tmp_path, reader)
    context = _context(role=AgentRole.REVIEWER, worktree_id=_WORKTREE_ID)
    request = ToolRequest(name=ToolName.VALIDATION_RESULTS_READ, arguments={})

    absent = await service.invoke(context, request)

    assert absent.status is ToolCallStatus.FAILED
    assert work.tool_calls.records[-1].authorized is True
    reader.error = EvidenceCorruptLineage("evidence lineage is corrupt")
    corrupt = await service.invoke(context, request)

    assert corrupt.status is ToolCallStatus.FAILED
    assert corrupt.error is not None
    assert corrupt.error.message == "controlled tool adapter failed"
    assert work.tool_calls.records[-1].authorized is True


async def test_evidence_tool_is_unavailable_without_reader(tmp_path: Path) -> None:
    service, work, git = _review_service(tmp_path, None)

    result = await service.invoke(
        _context(role=AgentRole.REVIEWER, worktree_id=_WORKTREE_ID),
        ToolRequest(name=ToolName.VALIDATION_RESULTS_READ, arguments={}),
    )

    assert result.status is ToolCallStatus.DENIED
    assert not git.calls
    assert work.tool_calls.records[-1].authorized is False


async def test_developer_cannot_probe_evidence_reader(tmp_path: Path) -> None:
    reader = _Reader()
    service, _, git = _review_service(tmp_path, reader)

    result = await service.invoke(
        _context(role=AgentRole.DEVELOPER, worktree_id=_WORKTREE_ID),
        ToolRequest(name=ToolName.VALIDATION_RESULTS_READ, arguments={}),
    )

    assert result.status is ToolCallStatus.DENIED
    assert not reader.scopes
    assert not git.calls


async def test_evidence_tool_rejects_model_scope_arguments(tmp_path: Path) -> None:
    service, _, git = _review_service(tmp_path, _Reader())

    result = await service.invoke(
        _context(role=AgentRole.REVIEWER, worktree_id=_WORKTREE_ID),
        ToolRequest(name=ToolName.VALIDATION_RESULTS_READ, arguments={"head_sha": _LIVE_HEAD}),
    )

    assert result.status is ToolCallStatus.DENIED
    assert not git.calls
