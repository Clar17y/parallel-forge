"""Preferences do not invalidate accepted validation or its publication authority."""

from dataclasses import replace
from functools import partial
from uuid import uuid4

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.runs import RunService
from forge.domain.evidence import decode_evidence_manifest
from forge.domain.run import RunSnapshot, RunState
from forge.domain.workflow import WorkflowPreferences
from forge.persistence.models import EvidenceSet
from forge.persistence.repositories.runs import ConcurrencyConflict
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_preparation import preparation_case
from test_subscription_publication import publication_case


def test_preferences_are_separate_from_lifecycle_identity():
    run = RunSnapshot(
        id=uuid4(), project_id=uuid4(), task_id=uuid4(), workflow=WorkflowPreferences()
    )
    changed = run.with_workflow(
        WorkflowPreferences(delegation="disabled", optional_review="required")
    )
    assert changed == run
    assert hash(changed) == hash(run)
    assert changed.workflow != run.workflow
    assert changed.version == run.version
    for updates in (
        {"version": 1},
        {"policy_version": 2},
        {"base_sha": "b" * 40},
        {"task_id": uuid4()},
        {"state": RunState.CANCELLED},
        {"branch_name": "other"},
    ):
        assert replace(changed, **updates) != run


@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["workflow", "version", "pause", "cancel", "evidence"])
async def test_accepted_validation_fences_lifecycle_but_allows_owner_preferences(
    session_factory, tmp_path, monkeypatch, mutation
):
    import test_subscription_review_selection as selection_module

    monkeypatch.setattr(
        selection_module, "preparation_case", partial(preparation_case, ordinary=True)
    )
    factory, proposal, dispatch, command, controller, runner, _ = await publication_case(
        session_factory, tmp_path
    )
    execute = runner.runner.run_terminal
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    edits = []

    async def during_check(request):
        terminal = await execute(request)
        async with factory() as work:
            before = await work.runs.get(command.run_id)
        if mutation == "workflow":
            service = RunService(factory)
            preferences = WorkflowPreferences(delegation="disabled", optional_review="required")
            updated = await service.update_workflow(
                actor=actor,
                run_id=before.id,
                idempotency_key="during-check",
                expected_run_version=before.version,
                expected_workflow=before.workflow,
                workflow=preferences,
            )
            assert updated.version == before.version and updated.workflow == preferences
            with pytest.raises(ConcurrencyConflict):
                await service.update_workflow(
                    actor=actor,
                    run_id=before.id,
                    idempotency_key="stale-during-check",
                    expected_run_version=before.version,
                    expected_workflow=before.workflow,
                    workflow=WorkflowPreferences(delegation="requested"),
                )
            edits.append(updated)
        else:
            async with factory() as work:
                if mutation in {"pause", "cancel"}:
                    await work.commands.enqueue(
                        run_id=before.id,
                        command_type=mutation,
                        idempotency_key="during-check-control",
                        payload={},
                        expected_run_version=before.version,
                        actor_id=actor.actor_id,
                    )
                elif mutation == "version":
                    from forge.persistence.models import Run

                    row = await work.session.get(Run, before.id)
                    row.version += 1
                else:
                    from forge.persistence.models import SubscriptionOperationBinding

                    binding = await work.session.scalar(
                        select(SubscriptionOperationBinding).where(
                            SubscriptionOperationBinding.attempt_id == proposal.attempt_id
                        )
                    )
                    binding.receipt_payload = {**binding.receipt_payload, "accepted": False}
                await work.commit()
        return terminal

    monkeypatch.setattr(runner.runner, "run_terminal", during_check)
    if mutation == "workflow":
        async with factory() as work:
            result = await controller.validate(command, work)
        assert result.state is RunState.AWAITING_PR_APPROVAL and len(edits) == 1
        async with factory() as work:
            validation = await work.evidence.get_by_id(
                result.validation_evidence_set_id, run_id=command.run_id
            )
            current = await work.runs.get(command.run_id)
            assert current.workflow == edits[0].workflow
            assert current.pending_gate.value == "pr"
        manifest = decode_evidence_manifest(
            await dispatch._store.open_bytes(validation.manifest_digest)
        )
        assert len(manifest.members) == 1 and manifest.members[0].check_name == "unit"
        assert manifest.members[0].controller_receipt_digest is not None
    else:
        with pytest.raises(CommandRecoveryRequired):
            async with factory() as work:
                await controller.validate(command, work)
        async with factory() as work:
            assert (
                await work.session.scalar(
                    select(EvidenceSet.id).where(EvidenceSet.run_id == command.run_id)
                )
                is None
            )
            assert (await work.runs.get(command.run_id)).state is RunState.VALIDATING
    assert runner.calls == runner.runner.calls == 1
