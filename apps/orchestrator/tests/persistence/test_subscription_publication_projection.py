"""The approval UI receives the same candidate and acceptance as the PR gate."""

import pytest
from forge.api.schemas.projections import CandidateSection
from forge.domain.approval import decode_pr_approval_evidence
from forge.persistence.queries.dashboard import DashboardQuery
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_publication import publication_case


@pytest.mark.integration
async def test_projection_exposes_linked_subscription_acceptance(session_factory, tmp_path):
    factory, proposal, dispatch, command, controller, _, _ = await publication_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        result = await controller.validate(command, work)
    evidence = decode_pr_approval_evidence(
        await dispatch._store.open_bytes(result.pr_evidence_digest)
    )
    projection = await DashboardQuery(session_factory).run_projection(command.run_id)
    assert projection is not None
    candidate = projection["candidate"]
    assert candidate == {
        "commit": proposal.review.candidate.head_sha,
        "pending_evidence_digest": result.pr_evidence_digest,
        "validation_evidence_digest": evidence.validation_digest,
        "review_evidence_digest": None,
        "acceptance_evidence_digest": evidence.acceptance_digest,
        "candidate_tree_digest": evidence.candidate_tree_digest,
    }
    assert CandidateSection.model_validate(candidate).model_dump() == candidate
