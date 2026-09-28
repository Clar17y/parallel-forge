"""Dashboard plan output follows the run's subscription approval evidence."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.application.services.plan_revision import PlanRevisionService
from forge.application.services.subscription_plan_gate import SubscriptionPlanGateService
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.persistence.models import ArtifactLineage, ArtifactLineageParent, Run
from forge.persistence.queries.dashboard import DashboardQuery
from sqlalchemy import select
from test_scheduler_acceptance import (  # noqa: F401 - pytest fixture
    _remove_disposable_subscription_rows,
)
from test_subscription_plan_gate import approve_proposal, proposal_case


@pytest.mark.integration
async def test_projection_exposes_plan_bound_to_pending_subscription_gate(
    session_factory, tmp_path
):
    factory, store, evidence, _plan, _validator = await proposal_case(session_factory, tmp_path)
    outcome = await SubscriptionPlanGateService(store, factory).request(evidence, _plan)

    projection = await DashboardQuery(session_factory).run_projection(evidence.producer.run_id)

    assert projection is not None
    assert projection["plan"]["approval_evidence_digest"] == outcome.evidence_digest
    assert projection["plan"]["output_artifact_digest"] == evidence.plan_digest


@pytest.mark.integration
async def test_projection_retains_approved_subscription_plan_after_gate_advances(
    session_factory, tmp_path
):
    factory, store, evidence, plan, validator = await proposal_case(session_factory, tmp_path)
    outcome = await SubscriptionPlanGateService(store, factory).request(evidence, plan)
    await approve_proposal(factory, session_factory, evidence, validator, outcome)

    projection = await DashboardQuery(session_factory).run_projection(evidence.producer.run_id)

    assert projection is not None
    assert projection["plan"]["approval_evidence_digest"] == outcome.evidence_digest
    assert projection["plan"]["output_artifact_digest"] == evidence.plan_digest


@pytest.mark.integration
async def test_projection_keeps_pending_plan_visible_across_pause_and_resume(
    session_factory, tmp_path
):
    factory, store, evidence, plan, _validator = await proposal_case(session_factory, tmp_path)
    await SubscriptionPlanGateService(store, factory).request(evidence, plan)
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        paused = await work.runs.pause(run.id, run.version, "run.paused", {})
        await work.commit()

    paused_projection = await DashboardQuery(session_factory).run_projection(
        evidence.producer.run_id
    )
    async with factory() as work:
        await work.runs.resume(paused.id, paused.version, "run.resumed", {})
        await work.commit()
    resumed_projection = await DashboardQuery(session_factory).run_projection(
        evidence.producer.run_id
    )

    assert paused_projection is not None and resumed_projection is not None
    assert paused_projection["plan"]["output_artifact_digest"] == evidence.plan_digest
    assert resumed_projection["plan"]["output_artifact_digest"] == evidence.plan_digest


@pytest.mark.integration
async def test_projection_does_not_select_another_runs_pending_plan(
    session_factory, tmp_path
):
    factory, store, evidence, plan, _validator = await proposal_case(
        session_factory, tmp_path / "first"
    )
    other_factory, other_store, other_evidence, other_plan, _ = await proposal_case(
        session_factory, tmp_path / "second"
    )
    await SubscriptionPlanGateService(store, factory).request(evidence, plan)
    other_outcome = await SubscriptionPlanGateService(other_store, other_factory).request(
        other_evidence, other_plan
    )
    async with factory() as work:
        run = await work.session.get(Run, evidence.producer.run_id)
        run.pending_evidence_digest = other_outcome.evidence_digest
        await work.commit()

    projection = await DashboardQuery(session_factory).run_projection(evidence.producer.run_id)
    other_projection = await DashboardQuery(session_factory).run_projection(
        other_evidence.producer.run_id
    )

    assert projection is not None and other_projection is not None
    assert projection["plan"]["output_artifact_digest"] is None
    assert other_projection["plan"]["output_artifact_digest"] == other_evidence.plan_digest


@pytest.mark.integration
async def test_projection_hides_old_plan_after_revision_while_other_run_keeps_its_plan(
    session_factory, tmp_path
):
    factory, store, evidence, plan, validator = await proposal_case(
        session_factory, tmp_path / "first"
    )
    other_factory, other_store, other_evidence, other_plan, _ = await proposal_case(
        session_factory, tmp_path / "second"
    )
    first_outcome = await SubscriptionPlanGateService(store, factory).request(evidence, plan)
    other_outcome = await SubscriptionPlanGateService(other_store, other_factory).request(
        other_evidence, other_plan
    )
    actor_id = uuid4()
    revision = CommandEnvelope(
        id=uuid4(),
        run_id=evidence.producer.run_id,
        command_type="request_plan_revision",
        idempotency_key=f"revise:{evidence.producer.run_id}",
        payload={"feedback": "Revise the proposal."},
        status=CommandStatus.LEASED,
        expected_run_version=first_outcome.run_version,
        actor_id=actor_id,
        payload_schema_version=1,
        attempt=1,
        available_at=datetime.now(UTC),
        lease_owner="revision-test",
        lease_expires_at=datetime.now(UTC),
    )
    async with factory() as work:
        await PlanRevisionService(store, validator).execute(revision, work)

    first_projection = await DashboardQuery(session_factory).run_projection(evidence.producer.run_id)
    other_projection = await DashboardQuery(session_factory).run_projection(
        other_evidence.producer.run_id
    )

    assert first_projection is not None and other_projection is not None
    assert first_projection["plan"]["output_artifact_digest"] is None
    assert other_projection["plan"]["approval_evidence_digest"] == other_outcome.evidence_digest
    assert other_projection["plan"]["output_artifact_digest"] == other_evidence.plan_digest


@pytest.mark.integration
@pytest.mark.parametrize("broken_binding", ["parent", "plan_attempt", "evidence_attempt"])
async def test_projection_requires_evidence_to_plan_artifact_lineage(
    session_factory, tmp_path, broken_binding
):
    factory, store, evidence, plan, _validator = await proposal_case(session_factory, tmp_path)
    await SubscriptionPlanGateService(store, factory).request(evidence, plan)
    async with factory() as work:
        evidence_lineage = await work.session.scalar(
            select(ArtifactLineage)
            .where(
                ArtifactLineage.run_id == evidence.producer.run_id,
                ArtifactLineage.producer_kind == "subscription_plan_approval_evidence",
                ArtifactLineage.producer_id == evidence.producer.attempt_id,
            )
        )
        if broken_binding == "parent":
            parent = await work.session.scalar(
                select(ArtifactLineageParent).where(
                    ArtifactLineageParent.run_id == evidence.producer.run_id,
                    ArtifactLineageParent.artifact_id == evidence_lineage.artifact_id,
                )
            )
            assert parent is not None
            await work.session.delete(parent)
        elif broken_binding == "evidence_attempt":
            evidence_lineage.producer_id = uuid4()
        else:
            plan_lineage = await work.session.scalar(
                select(ArtifactLineage).where(
                    ArtifactLineage.run_id == evidence.producer.run_id,
                    ArtifactLineage.producer_kind == "subscription_plan",
                    ArtifactLineage.producer_id == evidence.producer.attempt_id,
                )
            )
            assert plan_lineage is not None
            plan_lineage.producer_id = uuid4()
        await work.commit()

    projection = await DashboardQuery(session_factory).run_projection(evidence.producer.run_id)

    assert projection is not None
    assert projection["plan"]["output_artifact_digest"] is None
