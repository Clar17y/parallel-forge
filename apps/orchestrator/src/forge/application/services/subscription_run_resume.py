"""Restore subscription scheduling without inventing legacy provider deliveries."""

from typing import TYPE_CHECKING
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.subscription_delivery_ack import delivery_binding
from forge.domain.command import CommandEnvelope
from forge.domain.event import RunEvent
from forge.domain.operation import canonical_digest
from forge.domain.run import RunSnapshot, RunState
from forge.domain.subscription import SpecialistPurpose, encode_subscription_record

if TYPE_CHECKING:
    from forge.application.services.subscription_remote_remediation import (
        SubscriptionRemoteRemediationController,
    )

SCHEDULER_RESUME_STATES = frozenset(
    {RunState.PLANNING, RunState.IMPLEMENTING, RunState.REMEDIATING}
)


async def failed_repair_bindings(
    work: UnitOfWork,
    resume: CommandEnvelope,
    restored: RunState,
    store: ArtifactStore | None,
    repairs: SubscriptionRemoteRemediationController | None,
) -> tuple[dict[str, object], ...]:
    """Prove committed failed deliveries for the atomic resume event, without writes."""
    if (
        restored is not RunState.REMEDIATING
        or await work.subscription.envelope_for_run(resume.run_id) is None
    ):
        return ()
    sources = [
        source
        for source in await work.commands.list_failed_normal(
            run_id=resume.run_id, exclude_command_id=resume.id
        )
        if source.command_type in {"remediate_remote", "update_base"}
        and source.expected_run_version == resume.expected_run_version - 1
    ]
    if not sources:
        return ()
    if store is None or repairs is None:
        raise CommandRecoveryRequired("subscription repair resume verifier is unavailable")
    from forge.application.services.subscription_base_update import EVENT as BASE_EVENT
    from forge.application.services.subscription_remote_remediation import EVENT as REMOTE_EVENT

    approved = await ApprovedPlanLoader(store).load(work, resume.run_id)
    events = await work.events.list_after(resume.run_id, 0)
    bindings = []
    for source in sources:
        if source.command_type == "update_base":
            adoption = await repairs.base_updates.replay(source, work, approved)
            if adoption is None:
                continue
            if not adoption.repair.repaired or adoption.version != resume.expected_run_version - 1:
                raise CommandRecoveryRequired("subscription base repair pause source differs")
            event_type = BASE_EVENT
        else:
            decision = await repairs.replay(source, work, approved)
            if decision is None:
                await repairs.verify_unadmitted(source, work, approved)
                continue
            if (
                decision.state is not restored
                or decision.version != resume.expected_run_version - 1
            ):
                raise CommandRecoveryRequired("subscription remote repair pause source differs")
            event_type = REMOTE_EVENT
        receipts = [
            event
            for event in events
            if event.event_type == event_type
            and event.payload.get("source_command_id") == str(source.id)
        ]
        if len(receipts) != 1:
            raise CommandRecoveryRequired("subscription repair receipt is ambiguous")
        bindings.append(delivery_binding(source, receipts[0]))
    if len(bindings) > 1:
        raise CommandRecoveryRequired("subscription repair resume source is ambiguous")
    return tuple(bindings)


async def subscription_resume_binding(
    work: UnitOfWork, run: RunSnapshot, *, historical: bool = False
) -> dict[str, object] | None:
    """Bind the existing primary and immutable route envelope to a resume receipt.

    The caller proves quiescence and the pause/resume commands. This binding
    does not create an attempt, spend budget, grant paths or clear task controls.
    Replay permits subsequent scheduler progress without repeating any of it.
    """
    if (run.state if historical else run.suspended_state) not in SCHEDULER_RESUME_STATES:
        return None
    envelope = await work.subscription.envelope_for_run(run.id)
    if envelope is None:
        return None
    tasks = await work.subscription.invocation_tasks(run.id, UUID(int=0))
    primaries = [task for task in tasks if task.purpose is SpecialistPurpose.PRIMARY]
    if (
        len(primaries) != 1
        or primaries[0].parent_task_id is not None
        or not primaries[0].route.is_primary
        or envelope.run_id != run.id
        or envelope.safety_policy_version != run.policy_version
    ):
        raise CommandRecoveryRequired("subscription resume primary authority differs")
    if not historical:
        outcomes = await work.subscription.invocation_outcomes(
            run.id, tuple(task.task_id for task in tasks)
        )
        # The immediate caller reaches this branch only after its exact resume
        # lease has proved quiescence.  A retained, decision-pending primary
        # remains reconciling until the existing human-gate application runs;
        # it is a scheduler continuation, not work to redeliver.
        if not any(outcome.state in {"queued", "blocked", "reconciling"} for outcome in outcomes):
            raise CommandRecoveryRequired("subscription resume has no scheduled work")
    return {
        "kind": "subscription_scheduler",
        "primary_task_id": str(primaries[0].task_id),
        "envelope_digest": canonical_digest(encode_subscription_record(envelope)),
    }


async def settle_subscription_remote_deliveries(
    work: UnitOfWork,
    resume: CommandEnvelope,
    paused: RunSnapshot,
    store: ArtifactStore | None,
    repairs: SubscriptionRemoteRemediationController | None,
) -> None:
    """Acknowledge only a proved committed repair whose observed lease expired."""
    sources = [
        source
        for source in await work.commands.list_outstanding_normal(
            run_id=paused.id, exclude_command_id=resume.id
        )
        if source.command_type == "remediate_remote"
    ]
    if not sources or await work.subscription.envelope_for_run(paused.id) is None:
        return
    if store is None or repairs is None:
        raise CommandRecoveryRequired("subscription repair resume verifier is unavailable")
    approved = await ApprovedPlanLoader(store).load(work, paused.id)
    for source in sources:
        decision = await repairs.replay(source, work, approved)
        if decision is None:
            continue
        if (
            paused.state is not RunState.PAUSED
            or paused.suspended_state is not decision.state
            or decision.version != paused.version - 1
        ):
            raise CommandRecoveryRequired("subscription repair pause source differs")
        if await work.commands.complete_expired_observed_lease(source) is None:
            raise CommandRecoveryRequired("subscription repair command lease is active or changed")
        await work.events.append(
            RunEvent(
                run_id=paused.id,
                run_version=paused.version,
                event_type="subscription_remote_repair.acknowledged_on_resume",
                actor_class="worker",
                actor_id=resume.actor_id,
                payload={
                    "source_command_id": str(source.id),
                    "resume_command_id": str(resume.id),
                    "repair_version": decision.version,
                },
            )
        )
