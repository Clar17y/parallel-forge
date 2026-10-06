"""Subject-specific authoring, brief, and provider boundaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from forge.agents.client_process import ClientProcessReceipt, ClientProcessResult
from forge.domain.epic_brainstorm import (
    AuthoringJobSnapshot,
    AuthoringOutcome,
    AuthoringReceipt,
    BrainstormProposal,
    BrainstormTurn,
    FrozenBriefContent,
)
from forge.domain.epic_decomposition import DecompositionProposal
from forge.domain.subscription import AttemptTelemetry
from forge.tools.epic_brainstorm import BrainstormReadOnlyTools


class EpicAuthoringPort(Protocol):
    async def submit(self, job: AuthoringJobSnapshot) -> AuthoringReceipt: ...
    async def observe(self, job_id: UUID) -> AuthoringOutcome: ...


@dataclass(frozen=True, slots=True)
class BriefInput:
    epic_id: UUID
    project_id: UUID
    epic_version: int
    draft_digest: str
    accepted_revision_id: UUID | None
    accepted_digest: str | None
    draft_content: FrozenBriefContent = field(default_factory=FrozenBriefContent)
    accepted_content: FrozenBriefContent | None = None
    accepted_graph_revision_id: UUID | None = None
    accepted_graph_digest: str | None = None


class BrainstormBriefPort(Protocol):
    """Adapter to #69; implementations use the caller's database transaction."""

    async def input(self, epic_id: UUID, *, for_update: bool = False) -> BriefInput: ...
    async def save_proposal_revision(
        self,
        epic_id: UUID,
        *,
        expected_version: int,
        source_job_id: UUID,
        proposal: BrainstormProposal,
    ) -> UUID: ...


@dataclass(frozen=True, slots=True)
class BrainstormGatewayResult:
    proposal: BrainstormProposal | DecompositionProposal | None
    telemetry: AttemptTelemetry | None
    failure: str | None = None
    quota_reset_at: str | None = None


class AuthoringGatewayResult(Protocol):
    """Fields the shared worker consumes from either subject's gateway result."""

    @property
    def proposal(self) -> BrainstormProposal | DecompositionProposal | None: ...

    @property
    def telemetry(self) -> AttemptTelemetry | None: ...

    @property
    def failure(self) -> str | None: ...

    @property
    def quota_reset_at(self) -> str | None: ...


class BrainstormGateway(Protocol):
    async def execute(
        self,
        job: AuthoringJobSnapshot,
        turns: tuple[BrainstormTurn, ...],
        reader: BrainstormReadOnlyTools,
        *,
        cancelled: Callable[[], Awaitable[bool]],
        lifecycle: BrainstormProcessLifecycle,
    ) -> AuthoringGatewayResult: ...


class BrainstormProcessLifecycle(Protocol):
    async def launch_intent(self, launch_id: str) -> None: ...
    async def started(self, receipt: ClientProcessReceipt) -> None: ...
    async def finished(
        self, receipt: ClientProcessReceipt | None, result: ClientProcessResult | None
    ) -> None: ...


__all__ = [
    "AuthoringGatewayResult",
    "BrainstormBriefPort",
    "BrainstormGateway",
    "BrainstormGatewayResult",
    "BriefInput",
    "EpicAuthoringPort",
]
