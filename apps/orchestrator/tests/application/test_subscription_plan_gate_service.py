from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.services.subscription_plan_gate import SubscriptionPlanGateService
from forge.domain.approval import SubscriptionPlanApprovalEvidence, SubscriptionPlanProducer
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.plan import PlanOutput
from forge.domain.policy import RunnerMode


class _TamperedStore:
    def __init__(self, checked: list[bool]) -> None:
        self.checked = checked
        self.writes = 0

    async def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str,
        max_bytes: int | None = None,
        bounding_policy: str = "none",
    ) -> ArtifactDescriptor:
        assert self.checked == [True]
        self.writes += 1
        digest = hashlib.sha256(data).hexdigest()
        return ArtifactDescriptor(
            digest=digest,
            media_type=media_type,
            byte_count=len(data),
            storage_path=Path(f"sha256/{digest[:2]}/{digest[2:]}.blob"),
        )

    async def verify(self, digest: str) -> bool:
        return False

    async def open_bytes(self, digest: str, *, max_bytes: int | None = None) -> bytes:
        return b""


def _proposal() -> tuple[SubscriptionPlanApprovalEvidence, PlanOutput]:
    plan = PlanOutput(
        summary="Plan",
        assumptions=(),
        affected_components=("app",),
        steps=("do",),
        required_checks=("test",),
        risks=("risk",),
        security_considerations=(),
        dependency_changes=(),
    )
    digest = hashlib.sha256(plan.model_dump_json().encode()).hexdigest()
    producer = SubscriptionPlanProducer(
        attempt_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        plan_attempt=1,
        plan_digest=digest,
        task_digest="a" * 64,
        envelope_digest="b" * 64,
        budget_digest="c" * 64,
        route_digest="d" * 64,
        telemetry={"input_tokens": None, "output_tokens": None, "duration_ms": 1},
    )
    return SubscriptionPlanApprovalEvidence(
        task_version=1,
        task_digest="e" * 64,
        plan_digest=digest,
        repository="owner/repo",
        base_ref="refs/heads/main",
        base_sha="f" * 40,
        policy_version=1,
        runner_mode=RunnerMode.DOCKER,
        local_remediation_limit=0,
        token_budget=1,
        cost_budget_minor=0,
        duration_budget_seconds=1,
        producer=producer,
        result_digest="f" * 64,
    ), plan


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_active", [False, True])
async def test_subscription_plan_gate_fences_before_artifact_io_and_rejects_unverified_artifact(
    checkpoint_active: bool,
) -> None:
    evidence, plan = _proposal()
    checked: list[bool] = []

    async def get_run(run_id):
        assert run_id == evidence.producer.run_id
        return SimpleNamespace(id=run_id, project_id=uuid4())

    async def get_plan(attempt_id):
        assert attempt_id == evidence.producer.attempt_id

    async def active_for_subject(subject):
        assert subject.subject_id == evidence.producer.run_id
        checked.append(True)
        return object() if checkpoint_active else None

    @asynccontextmanager
    async def work_factory():
        work = SimpleNamespace(
            runs=SimpleNamespace(get=get_run),
            subscription_plan_gate=SimpleNamespace(get=get_plan),
            task_usage_checkpoints=SimpleNamespace(active_for_subject=active_for_subject),
        )
        yield work

    artifacts = _TamperedStore(checked)
    service = SubscriptionPlanGateService(artifacts, work_factory)
    with pytest.raises(
        RuntimeError, match="usage checkpoint" if checkpoint_active else "not verified"
    ):
        await service.request(evidence, plan)
    assert checked == [True]
    assert artifacts.writes == (0 if checkpoint_active else 2)
