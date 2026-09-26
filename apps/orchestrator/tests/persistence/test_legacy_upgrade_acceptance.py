"""Retained legacy evidence is checked at the actual v0.1 schema boundary."""

import pytest
from forge.application.services.auth import AuthService
from forge.domain.actor import AgentRole
from forge.domain.approval import PlanApprovalEvidence, canonical_digest
from forge.domain.run import RunState
from legacy_upgrade_case import legacy_plan_case
from legacy_upgrade_controls import replay_legacy_plan_controls
from legacy_upgrade_execution import unfinished_legacy_case, verify_unfinished_recovery
from legacy_upgrade_manifest import retain_legacy_manifest, upgrade_legacy_case
from legacy_upgrade_review import replay_review_controls, reviewed_legacy_case


@pytest.mark.integration
async def test_legacy_plan_artifacts_survive_v01_schema_and_restart(
    session_factory, migrated_database_url, alembic_config_factory, tmp_path
):
    case = await legacy_plan_case(session_factory, tmp_path)
    try:
        assert type(case.evidence) is PlanApprovalEvidence
        assert case.run.state is RunState.AWAITING_PLAN_APPROVAL
        digests = (case.evidence.plan_digest, case.run.pending_evidence_digest)
        original = {digest: await case.store.open_bytes(digest) for digest in digests}
        auth = AuthService(case.factory)
        operator_session = await auth.exchange_bootstrap(await auth.issue_bootstrap())
        config = alembic_config_factory(migrated_database_url)
        await upgrade_legacy_case(case, session_factory, migrated_database_url, config)
        async with case.factory() as work:
            run = await work.runs.get(case.run.id)
            assert run == case.run
            assert await work.subscription.envelope_for_run(run.id) is None
            evidence = await case.validator.validate(work, run.id)
            assert canonical_digest(evidence) == run.pending_evidence_digest
        assert {digest: await case.store.open_bytes(digest) for digest in digests} == original
        assert len(case.gateway.requests) == 1
        controls = await replay_legacy_plan_controls(case, session_factory, operator_session)
        assert controls[-1]["state"] == "PREPARING_WORKTREE"
        assert {digest: await case.store.open_bytes(digest) for digest in digests} == original
        await retain_legacy_manifest(
            case, session_factory, tmp_path, scenario="A9-plan", controls=controls
        )
    finally:
        await case.handlers.aclose()


@pytest.mark.integration
async def test_legacy_unfinished_execution_preserves_write_receipt_after_upgrade(
    session_factory, migrated_database_url, alembic_config_factory, tmp_path
):
    case = await unfinished_legacy_case(session_factory, tmp_path)
    try:
        config = alembic_config_factory(migrated_database_url)
        await upgrade_legacy_case(case, session_factory, migrated_database_url, config)
        await verify_unfinished_recovery(case, session_factory)
        await retain_legacy_manifest(case, session_factory, tmp_path, scenario="A9-unfinished")
    finally:
        await case.handlers.aclose()


@pytest.mark.integration
@pytest.mark.parametrize("control", ["pause", "cancel"])
async def test_legacy_retained_review_and_pending_control_survive_upgrade(
    session_factory, migrated_database_url, alembic_config_factory, tmp_path, control
):
    case = await reviewed_legacy_case(session_factory, tmp_path, control=control)
    try:
        config = alembic_config_factory(migrated_database_url)
        await upgrade_legacy_case(case, session_factory, migrated_database_url, config)
        assert len(case.legacy_rows["validation_results"]) == 2
        assert all(row["status"] == "PASSED" for row in case.legacy_rows["validation_results"])
        # The reviews table stores findings, so an approving empty report has
        # none. The actual report is execution-bound immutable evidence.
        assert case.legacy_rows["reviews"] == []
        reviewer = [
            row
            for row in case.legacy_rows["agent_executions"]
            if row["role"] == AgentRole.REVIEWER.value
        ]
        assert len(reviewer) == 1 and reviewer[0]["output_artifact_id"] is not None
        assert {row["kind"] for row in case.legacy_rows["evidence_sets"]} == {
            "validation",
            "review",
        }
        controls = await replay_review_controls(case, session_factory, control=control)
        await retain_legacy_manifest(
            case, session_factory, tmp_path, scenario=f"A9-review-{control}", controls=controls
        )
    finally:
        await case.handlers.aclose()
