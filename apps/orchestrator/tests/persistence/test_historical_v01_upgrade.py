"""Actual v0.1 application code produces evidence consumed after the v0.2 upgrade."""

import pytest
from forge.application.services.auth import AuthService
from forge.domain.approval import PlanApprovalEvidence, canonical_digest
from forge.domain.run import RunState
from historical_v01 import historical_plan_case
from legacy_upgrade_controls import replay_legacy_plan_controls
from legacy_upgrade_manifest import retain_legacy_manifest


@pytest.mark.integration
async def test_historical_v01_plan_survives_upgrade_and_current_operator_controls(
    test_database_url, alembic_config_factory, tmp_path
):
    case = await historical_plan_case(test_database_url, alembic_config_factory, tmp_path)
    try:
        assert case.run.state is RunState.AWAITING_PLAN_APPROVAL
        assert type(case.evidence) is PlanApprovalEvidence
        assert len(case.gateway.requests) == 1
        assert case.source_provenance["revision"] == "781567f7365e76dfc37cf8e7cf98765cb00800e4"
        assert len(case.source_provenance["processes"]) == 2
        assert all(row["terminal"]["stop_confirmed"] for row in case.source_provenance["processes"])
        assert case.source_provenance["old_reader_after_head_upgrade"] is True
        async with case.factory() as work:
            assert await work.subscription.envelope_for_run(case.run.id) is None
            evidence = await case.validator.validate(work, case.run.id)
            assert canonical_digest(evidence) == case.run.pending_evidence_digest
        auth = AuthService(case.factory)
        operator_session = await auth.exchange_bootstrap(await auth.issue_bootstrap())
        controls = await replay_legacy_plan_controls(case, case.session_factory, operator_session)
        assert controls[-1]["state"] == "PREPARING_WORKTREE"
        await retain_legacy_manifest(
            case,
            case.session_factory,
            tmp_path,
            scenario="A9-historical-v01-plan",
            controls=controls,
            source_provenance=case.source_provenance,
        )
    finally:
        await case.handlers.aclose()
        await case.engine.dispose()
