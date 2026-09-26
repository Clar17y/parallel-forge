from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.services.subscription_plan_gate import SubscriptionPlanGateService
from forge.domain.approval import SubscriptionPlanApprovalEvidence, SubscriptionPlanProducer
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.plan import PlanOutput
from forge.domain.policy import RunnerMode


class _TamperedStore:
    async def put_bytes(self, data: bytes, *, media_type: str, max_bytes: int | None = None, bounding_policy: str = "none") -> ArtifactDescriptor:
        digest = hashlib.sha256(data).hexdigest()
        return ArtifactDescriptor(digest=digest, media_type=media_type, byte_count=len(data), storage_path=Path(f"sha256/{digest[:2]}/{digest[2:]}.blob"))

    async def verify(self, digest: str) -> bool:
        return False

    async def open_bytes(self, digest: str, *, max_bytes: int | None = None) -> bytes:
        return b""


def _proposal() -> tuple[SubscriptionPlanApprovalEvidence, PlanOutput]:
    plan = PlanOutput(summary="Plan", assumptions=(), affected_components=("app",), steps=("do",), required_checks=("test",), risks=("risk",), security_considerations=(), dependency_changes=())
    digest = hashlib.sha256(plan.model_dump_json().encode()).hexdigest()
    producer = SubscriptionPlanProducer(attempt_id=uuid4(), run_id=uuid4(), task_id=uuid4(), plan_attempt=1, plan_digest=digest, task_digest="a" * 64, envelope_digest="b" * 64, budget_digest="c" * 64, route_digest="d" * 64, telemetry={"input_tokens": None, "output_tokens": None, "duration_ms": 1})
    return SubscriptionPlanApprovalEvidence(task_version=1, task_digest="e" * 64, plan_digest=digest, repository="owner/repo", base_ref="refs/heads/main", base_sha="f" * 40, policy_version=1, runner_mode=RunnerMode.DOCKER, local_remediation_limit=0, token_budget=1, cost_budget_minor=0, duration_budget_seconds=1, producer=producer, result_digest="f" * 64), plan


@pytest.mark.asyncio
async def test_subscription_plan_gate_rejects_unverified_artifact_before_opening_transaction() -> None:
    evidence, plan = _proposal()
    service = SubscriptionPlanGateService(_TamperedStore(), lambda: (_ for _ in ()).throw(AssertionError("uow")))
    with pytest.raises(RuntimeError, match="not verified"):
        await service.request(evidence, plan)
