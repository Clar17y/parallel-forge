"""Providerless decomposition gateway backed by a real supervised child process."""

from __future__ import annotations

import sys
from uuid import uuid4

from forge.agents.client_process import ClientLaunchSpec, ClientProcessSupervisor
from forge.application.ports.epic_decomposition import DecompositionGatewayResult
from forge.domain.epic_brainstorm import AuthoringJobSnapshot
from forge.domain.epic_decomposition import DecompositionProposal
from forge.domain.epic_items import ItemInput
from forge.domain.subscription import AttemptTelemetry


def proposal_for(job: AuthoringJobSnapshot) -> DecompositionProposal:
    assert job.accepted_content is not None
    assert job.input_brief_revision_id is not None
    assert job.input_brief_digest is not None
    items = tuple(
        ItemInput(
            item_id=uuid4(),
            disposition="required",
            ordinal=index,
            title=f"Work item {index + 1}",
            outcome=f"Deliver {requirement.text[:64]}",
            acceptance_criteria=list(requirement.acceptance_criteria) or ["Accepted"],
            source_requirement_ids=[requirement.requirement_id],
            dependency_item_ids=[],
        )
        for index, requirement in enumerate(job.accepted_content.requirements)
    )
    return DecompositionProposal(
        turn_id=uuid4(),
        epic_id=job.epic_id,
        project_id=job.project_id,
        brief_revision_id=job.input_brief_revision_id,
        brief_digest=job.input_brief_digest,
        items=items,
        summary="Proposed breakdown",
        assumptions=job.accepted_content.assumptions,
        open_questions=job.accepted_content.open_questions,
    )


class SupervisedGateway:
    def __init__(self, transform=None) -> None:
        self.transform = transform

    async def execute(self, job, turns, reader, *, cancelled, lifecycle):
        if await cancelled():
            return DecompositionGatewayResult(None, None, "cancelled")
        spec = ClientLaunchSpec(
            argv=(
                sys.executable,
                "-c",
                "import sys,json; sys.stdin.readline(); print(json.dumps({'ok': True}))",
            ),
            cwd=".",
            environment={},
            duration_seconds=5,
        )
        await ClientProcessSupervisor().run(
            spec, {"prompt": turns[-1].text if turns else "decompose"}, lifecycle=lifecycle
        )
        proposal = proposal_for(job)
        if self.transform is not None:
            proposal = self.transform(proposal)
        return DecompositionGatewayResult(
            proposal=proposal,
            telemetry=AttemptTelemetry(input_tokens=4, output_tokens=5, duration_ms=10),
        )
