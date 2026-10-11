"""Authenticated, idempotent owner revisions for project monitoring settings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from forge.application.ports.audit import AuditRepository
from forge.application.ports.executions import ExecutionAdmission
from forge.application.ports.mutations import MutationRepository
from forge.application.ports.projects import ProjectRepository
from forge.application.ports.task_usage import MonitoringPolicyRepository
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.domain.agent import AgentBudget
from forge.domain.policy import AgentModelPolicy
from forge.domain.task_usage_contract import MonitoringMode, MonitoringPolicy


class MonitoringPolicyUnitOfWork(Protocol):
    projects: ProjectRepository
    mutations: MutationRepository
    audit: AuditRepository
    task_usage_policy: MonitoringPolicyRepository

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


class MonitoringPolicyRevisionRequest(BaseModel):
    """Omitted fields retain their value; explicit null clears optional caps/estimate."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(ge=0, le=2**63 - 2)
    mode: MonitoringMode | None = None
    warning_multiplier: float | None = Field(default=None, ge=1)
    checkpoint_multiplier: float | None = Field(default=None, ge=1)
    history_limit: StrictInt | None = Field(default=None, ge=1, le=2**63 - 1)
    window_days: StrictInt | None = Field(default=None, ge=1, le=2**63 - 1)
    minimum_comparables: StrictInt | None = Field(default=None, ge=1, le=2**63 - 1)
    hard_token_cap: StrictInt | None = Field(default=None, ge=1, le=2**63 - 1)
    hard_context_cap: StrictInt | None = Field(default=None, ge=1, le=2**63 - 1)
    manual_estimate_tokens: StrictInt | None = Field(default=None, ge=1, le=2**63 - 1)
    allow_cross_project_fallback: StrictBool | None = None
    note: StrictStr | None = Field(default=None, max_length=2000)


@dataclass(frozen=True, slots=True)
class MonitoringPolicyRevisionResult:
    policy: MonitoringPolicy
    actor_id: UUID
    affected_snapshot: str
    action: str = "set_policy"
    warnings: tuple[str, ...] = ()
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class EffectiveMonitoringPolicy:
    """Admission-time read, separate from an immutable agent execution envelope."""

    policy: MonitoringPolicy | None
    hard_token_cap: int | None
    cap_source: str


def resolve_effective_monitoring_policy(
    policy: MonitoringPolicy | None, *, frozen_hard_token_cap: int | None = None
) -> EffectiveMonitoringPolicy:
    """A later owner revision supersedes a legacy aggregate cap for new work only."""
    if frozen_hard_token_cap is not None and (
        type(frozen_hard_token_cap) is not int or not 1 <= frozen_hard_token_cap <= 2**63 - 1
    ):
        raise ValueError("frozen hard token cap is invalid")
    if policy is None:
        return EffectiveMonitoringPolicy(None, frozen_hard_token_cap, "frozen_legacy")
    return EffectiveMonitoringPolicy(policy, policy.hard_token_cap, "monitoring_revision")


async def resolve_agent_budget_for_admission(
    work: object, project_id: UUID, model_policy: AgentModelPolicy
) -> AgentBudget:
    """Freeze current project hard cap in a new request, retaining role caps."""
    budget = AgentBudget.from_model_policy(model_policy)
    repository = getattr(work, "task_usage_policy", None)
    if repository is None:
        # Older project/fixture UoWs have no monitoring settings.
        return budget
    policy: MonitoringPolicy | None = await repository.get(project_id)
    if policy is None:
        return budget
    return budget.model_copy(
        update={
            "max_total_tokens": policy.hard_token_cap,
            "monitoring_policy_revision": policy.revision,
        }
    )


def budget_for_frozen_admission(
    model_policy: AgentModelPolicy, admission: ExecutionAdmission
) -> AgentBudget:
    """Rebuild role limits with the monitoring fields frozen at admission."""
    if not isinstance(admission, ExecutionAdmission):
        raise TypeError("admission must be an ExecutionAdmission")
    return AgentBudget.from_model_policy(model_policy).model_copy(
        update={
            "max_total_tokens": admission.hard_token_cap,
            "monitoring_policy_revision": admission.monitoring_policy_revision,
        }
    )


def _snapshot(policy: MonitoringPolicy) -> str:
    payload = json.dumps(asdict(policy), default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class TaskUsagePolicyService:
    def __init__(self, unit_of_work_factory: Callable[[], MonitoringPolicyUnitOfWork]) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def get(self, project_id: UUID, revision: int | None = None) -> MonitoringPolicy | None:
        async with self._unit_of_work_factory() as work:
            await work.projects.get(project_id)
            policy = await work.task_usage_policy.get(project_id, revision)
            await work.commit()
            return policy

    async def resolve_for_admission(
        self, project_id: UUID, *, frozen_hard_token_cap: int | None = None
    ) -> EffectiveMonitoringPolicy:
        """Read the current revision for a future admission without rewriting history."""
        current = await self.get(project_id)
        return resolve_effective_monitoring_policy(
            current, frozen_hard_token_cap=frozen_hard_token_cap
        )

    async def revise(
        self,
        *,
        actor: AuthenticatedActor | LocalOperatorProfileActor,
        project_id: UUID,
        idempotency_key: str,
        request: MonitoringPolicyRevisionRequest,
    ) -> MonitoringPolicyRevisionResult:
        if actor.actor_class != "operator" or not isinstance(actor.actor_id, UUID):
            raise TypeError("authenticated owner identity is required")
        # Omission preserves a setting; explicit null clears one. Hash exactly
        # the submitted fields so one key cannot replay the other operation.
        payload = request.model_dump(mode="json", exclude_unset=True)
        request_digest = hashlib.sha256(
            json.dumps(
                {"project_id": str(project_id), "request": payload},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="project.monitoring_policy.revise",
                scope=f"project:{project_id}",
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if receipt.is_replay:
                version = (
                    None
                    if receipt.response_payload is None
                    else receipt.response_payload.get("revision")
                )
                if type(version) is not int or version < 1:
                    raise RuntimeError("monitoring revision receipt is unavailable")
                replayed = await work.task_usage_policy.get(project_id, version)
                if replayed is None:
                    raise RuntimeError("monitoring revision receipt refers to missing policy")
                await work.commit()
                return MonitoringPolicyRevisionResult(
                    policy=replayed,
                    actor_id=actor.actor_id,
                    affected_snapshot=_snapshot(replayed),
                    replayed=True,
                )

            await work.projects.get(project_id, for_update=True)
            current = await work.task_usage_policy.get(project_id)
            baseline = current or MonitoringPolicy(
                project_id=project_id, mode=MonitoringMode.CHECKPOINT
            )
            changes: dict[str, Any] = {}
            for name in (
                "mode",
                "warning_multiplier",
                "checkpoint_multiplier",
                "history_limit",
                "window_days",
                "minimum_comparables",
                "hard_token_cap",
                "hard_context_cap",
                "manual_estimate_tokens",
                "allow_cross_project_fallback",
            ):
                if name in request.model_fields_set:
                    value = getattr(request, name)
                    if value is None and name not in {
                        "hard_token_cap",
                        "hard_context_cap",
                        "manual_estimate_tokens",
                        "window_days",
                    }:
                        raise ValueError(f"{name} cannot be null")
                    changes[name] = value
            updated = replace(baseline, revision=request.expected_revision + 1, **changes)
            await work.task_usage_policy.append_policy_revision(
                updated, expected_revision=request.expected_revision, actor_id=actor.actor_id
            )
            audit_payload: dict[str, object] = {
                "request_digest": request_digest,
                "expected_revision": request.expected_revision,
                "revision": updated.revision,
                "action": "set_policy",
                "affected_snapshot": _snapshot(updated),
                "warnings": [],
            }
            if request.note is not None:
                audit_payload["note"] = request.note
            await work.audit.append(
                actor_id=actor.actor_id,
                event_type="project.monitoring_policy_revised",
                subject_type="project",
                subject_id=project_id,
                correlation_id=receipt.id,
                payload=audit_payload,
            )
            await work.mutations.complete(
                receipt.id,
                response_status=201,
                response_payload={"project_id": str(project_id), "revision": updated.revision},
                resource_kind="project_monitoring_policy",
                resource_id=project_id,
            )
            await work.commit()
            return MonitoringPolicyRevisionResult(
                policy=updated, actor_id=actor.actor_id, affected_snapshot=_snapshot(updated)
            )
