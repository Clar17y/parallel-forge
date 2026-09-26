"""Durable subscription execution admission contracts."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.domain.command import CommandEnvelope
from forge.domain.scheduling import TaskLease
from forge.domain.subscription import AttemptIdentity, ExecutionEnvelope, LogicalTaskContract


@dataclass(frozen=True, slots=True)
class SubscriptionAdmission:
    lease: TaskLease
    task: LogicalTaskContract
    attempt: AttemptIdentity
    envelope: ExecutionEnvelope
    candidate_epoch: int
    task_version: int


@dataclass(frozen=True, slots=True)
class SubscriptionSettlement:
    accepted: bool
    disposition: str
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class SubscriptionInvocationContext:
    worktree_id: str
    candidate_closed: bool


@dataclass(frozen=True, slots=True)
class SubscriptionResumption:
    attempt_id: UUID
    application_digest: str

    def payload(self) -> dict[str, object]:
        return {"attempt_id": str(self.attempt_id), "application_digest": self.application_digest}


class SubscriptionExecutionRepository(Protocol):
    async def resume_paused_attempts(
        self, resume: CommandEnvelope, pause: CommandEnvelope
    ) -> tuple[SubscriptionResumption, ...]: ...
    async def verify_paused_attempts(
        self, resume: CommandEnvelope
    ) -> tuple[SubscriptionResumption, ...]: ...
    async def invocation_context(
        self, admission: SubscriptionAdmission
    ) -> SubscriptionInvocationContext: ...
    async def settle(
        self, admission: SubscriptionAdmission, result: SubscriptionInvocationResult
    ) -> SubscriptionSettlement: ...
    async def admit(self, lease: TaskLease, attempt_id: UUID) -> SubscriptionAdmission: ...
