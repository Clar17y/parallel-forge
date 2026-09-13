"""Current recovery consumes admitted writes and review gates from actual v0.1."""

import pytest
from forge.domain.actor import AgentRole
from historical_v01 import historical_execution_case, historical_review_case
from legacy_upgrade_execution import verify_unfinished_recovery
from legacy_upgrade_manifest import retain_legacy_manifest
from legacy_upgrade_review import replay_review_controls


@pytest.mark.integration
async def test_historical_unfinished_write_recovers_once_after_upgrade(
    test_database_url, alembic_config_factory, tmp_path
):
    case = await historical_execution_case(test_database_url, alembic_config_factory, tmp_path)
    try:
        await verify_unfinished_recovery(case, case.session_factory)
        await retain_legacy_manifest(
            case,
            case.session_factory,
            tmp_path,
            scenario="A9-historical-v01-unfinished",
            source_provenance=case.source_provenance,
        )
    finally:
        await case.handlers.aclose()
        await case.engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("control", ["pause", "cancel"])
async def test_historical_review_and_pending_control_survive_upgrade(
    test_database_url, alembic_config_factory, tmp_path, control
):
    case = await historical_review_case(
        test_database_url, alembic_config_factory, tmp_path, control=control
    )
    try:
        assert len(case.legacy_rows["validation_results"]) == 2
        assert all(row["status"] == "PASSED" for row in case.legacy_rows["validation_results"])
        reviewers = [
            row
            for row in case.legacy_rows["agent_executions"]
            if row["role"] == AgentRole.REVIEWER.value
        ]
        assert len(reviewers) == 1 and reviewers[0]["output_artifact_id"] is not None
        assert {row["kind"] for row in case.legacy_rows["evidence_sets"]} == {
            "validation",
            "review",
        }
        controls = await replay_review_controls(case, case.session_factory, control=control)
        case.source_provenance["old_session_authenticated_after_upgrade"] = True
        manifest = await retain_legacy_manifest(
            case,
            case.session_factory,
            tmp_path,
            scenario=f"A9-historical-v01-review-{control}",
            controls=controls,
            source_provenance=case.source_provenance,
        )
        retained = manifest.read_text()
        assert all(
            value not in retained
            for value in (case.operator_session.session_token, case.operator_session.csrf_token)
        )
    finally:
        await case.handlers.aclose()
        await case.engine.dispose()
