"""Application port protocols and records for epic decomposition."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, Self
from uuid import UUID

from forge.application.ports.audit import AuditRepository
from forge.application.ports.epic_brainstorm import (
    AuthoringGatewayResult,
    BrainstormProcessLifecycle,
)
from forge.application.ports.epic_brief import EpicBriefRepository
from forge.application.ports.epic_items import EpicItemsRepository
from forge.application.ports.mutations import MutationRepository
from forge.application.services.auth import AuthenticatedActor
from forge.domain.epic_brainstorm import (
    AuthoringJobSnapshot,
    AuthoringOutcome,
    AuthoringReceipt,
    BrainstormThread,
    BrainstormTurn,
)
from forge.domain.epic_decomposition import DecompositionProposal
from forge.domain.subscription import AttemptTelemetry
from forge.tools.epic_brainstorm import BrainstormReadOnlyTools


@dataclass(frozen=True, slots=True)
class DecompositionGatewayResult:
    proposal: DecompositionProposal | None
    telemetry: AttemptTelemetry | None
    failure: str | None = None
    quota_reset_at: str | None = None


class DecompositionGateway(Protocol):
    async def execute(
        self,
        job: AuthoringJobSnapshot,
        turns: tuple[BrainstormTurn, ...],
        reader: BrainstormReadOnlyTools,
        *,
        cancelled: Callable[[], Awaitable[bool]],
        lifecycle: BrainstormProcessLifecycle,
    ) -> AuthoringGatewayResult: ...


class DecompositionAuthoringPort(Protocol):
    """Shared authoring hooks required by the decomposition subject."""

    async def create(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        actor: AuthenticatedActor,
        key: str,
        text: str,
    ) -> tuple[UUID, int]: ...

    async def turns(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        conversation_id: UUID,
    ) -> tuple[BrainstormTurn, ...]: ...

    async def threads(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        kind: Literal["brainstorm", "decomposition"] = "decomposition",
    ) -> tuple[BrainstormThread, ...]: ...

    async def append(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        conversation_id: UUID,
        expected_version: int,
        actor: AuthenticatedActor,
        key: str,
        text: str,
        pending: bool = False,
    ) -> int: ...

    async def submit(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        conversation_id: UUID,
        prompt_turn_id: UUID,
        expected_epic_version: int,
        expected_conversation_version: int,
        actor: AuthenticatedActor,
        key: str,
        kind: Literal["brainstorm", "decomposition"],
    ) -> AuthoringReceipt: ...

    async def require_kind(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        job_id: UUID,
        kind: Literal["brainstorm", "decomposition"],
    ) -> None: ...

    async def observe(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        job_id: UUID,
        kind: Literal["brainstorm", "decomposition"] | None = None,
    ) -> AuthoringOutcome: ...

    async def cancel(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        job_id: UUID,
        expected_job_version: int,
        actor: AuthenticatedActor,
        key: str,
    ) -> AuthoringReceipt: ...

    async def retry(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        job_id: UUID,
        expected_job_version: int,
        actor: AuthenticatedActor,
        key: str,
        owner_override: bool = False,
        override_note: str | None = None,
    ) -> AuthoringReceipt: ...


class EpicDecompositionJobRepository(Protocol):
    """Port for locking and updating authoring jobs during decomposition adoption."""

    async def lock_job_for_adoption(
        self,
        job_id: UUID,
        *,
        epic_id: UUID,
        project_id: UUID,
        expected_job_version: int,
        proposal_digest: str,
    ) -> LockedDecompositionProposal: ...

    async def mark_job_adopted(
        self,
        job_id: UUID,
        *,
        graph_revision_id: UUID,
        job_version: int,
    ) -> None: ...


class EpicDecompositionUnitOfWork(Protocol):
    epics: EpicBriefRepository
    epic_items: EpicItemsRepository
    mutations: MutationRepository
    audit: AuditRepository
    jobs: EpicDecompositionJobRepository

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DecompositionAdoptionResult:
    graph_revision_id: UUID
    graph_digest: str
    epic_version: int
    job_version: int


@dataclass(frozen=True, slots=True)
class LockedDecompositionProposal:
    snapshot: AuthoringJobSnapshot
    proposal: DecompositionProposal


__all__ = [
    "DecompositionAdoptionResult",
    "DecompositionAuthoringPort",
    "DecompositionGateway",
    "DecompositionGatewayResult",
    "EpicDecompositionJobRepository",
    "EpicDecompositionUnitOfWork",
    "LockedDecompositionProposal",
]
