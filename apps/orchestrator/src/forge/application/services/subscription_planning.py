"""Atomic initial subscription planning admission, with no provider or filesystem IO."""

from uuid import UUID, uuid5

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.control_settlement import pending_current_control_stop
from forge.domain.command import CommandEnvelope
from forge.domain.event import thaw_payload
from forge.domain.operation import canonical_digest
from forge.domain.run import RunState
from forge.domain.scheduling import ScheduleTask
from forge.domain.subscription import (
    AcceptanceCriterion,
    LogicalTaskContract,
    SpecialistPurpose,
    TaskBudget,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.domain.tool import repository_resource_identity

_EVENT = "run.subscription_planning_started"


class SubscriptionPlanningService:
    def __init__(self, primary_budget: TaskBudget) -> None:
        self._budget = primary_budget

    async def execute(self, command: CommandEnvelope, work: UnitOfWork) -> UUID:
        fenced = await work.commands.assert_current_lease(command)
        if (
            fenced.command_type != "start_planning"
            or fenced.run_id != command.run_id
            or command.command_type != fenced.command_type
            or fenced.idempotency_key != f"{command.run_id}:start-planning"
            or command.idempotency_key != fenced.idempotency_key
            or command.payload != fenced.payload
            or fenced.payload != {}
            or command.payload_schema_version != 1
            or fenced.payload_schema_version != 1
            or command.expected_run_version != fenced.expected_run_version
            or command.actor_id != fenced.actor_id
        ):
            raise CommandRecoveryRequired("subscription planning command binding differs")
        run = await work.runs.get_for_update(command.run_id)
        envelope = await work.subscription.envelope_for_run(run.id)
        if envelope is None or envelope.safety_policy_version != run.policy_version:
            raise CommandRecoveryRequired("subscription planning envelope is unavailable")
        primary_id = uuid5(run.id, "forge-subscription-primary-v1")
        resource = repository_resource_identity(run.project_id)
        envelope_digest = canonical_digest(encode_subscription_record(envelope))
        events = [
            event for event in await work.events.list_after(run.id, 0) if event.event_type == _EVENT
        ]
        if events:
            event = events[0]
            try:
                original = decode_subscription_record(thaw_payload(event.payload["primary_task"]))
                current = await work.subscription.get_task(run.id, primary_id)
                valid = (
                    len(events) == 1
                    and event.actor_class == "worker"
                    and event.run_version == command.expected_run_version + 1
                    and event.payload["command_id"] == str(command.id)
                    and event.payload["envelope_digest"] == envelope_digest
                    and event.payload["resource_id"] == resource
                    and isinstance(original, LogicalTaskContract)
                    and original.run_id == run.id
                    and original.task_id == primary_id
                    and original.purpose is SpecialistPurpose.PRIMARY
                    and original.parent_task_id is None
                    and not original.owned_paths
                    and original.route == envelope.route_for(SpecialistPurpose.PRIMARY)
                    and current.run_id == run.id
                    and current.task_id == primary_id
                    and current.purpose is SpecialistPurpose.PRIMARY
                    and current.parent_task_id is None
                    and current.route == original.route
                    and current.budget == original.budget
                )
            except KeyError, TypeError, ValueError:
                valid = False
            if not valid:
                raise CommandRecoveryRequired("subscription planning replay differs")
            await work.commit()
            return primary_id
        if (
            run.state is not RunState.CREATED
            or run.version != command.expected_run_version
            or run.pending_gate is not None
            or await pending_current_control_stop(work, run)
        ):
            raise CommandRecoveryRequired("subscription planning admission is no longer current")
        task = await work.tasks.get(run.task_id, for_update=True)
        if task.project_id != run.project_id:
            raise CommandRecoveryRequired("subscription planning task binding differs")
        primary = LogicalTaskContract(
            run_id=run.id,
            task_id=primary_id,
            purpose=SpecialistPurpose.PRIMARY,
            route=envelope.route_for(SpecialistPurpose.PRIMARY),
            budget=self._budget,
            max_repairs=self._budget.max_repairs,
            typed_acceptance=(
                AcceptanceCriterion(
                    criterion_id="approved-plan",
                    description="Produce an evidence-bound plan for the requested task.",
                ),
            ),
            untrusted_context_refs=(f"task:{task.id}:{task.task_digest}",),
        )
        if primary.budget.billing_mode is not primary.route.effective.billing_mode:
            raise CommandRecoveryRequired("subscription primary budget billing differs")
        await work.subscription.create_task(primary, idempotency_key=f"primary:{run.id}")
        await work.scheduler.admit_run(run.id)
        await work.scheduler.enqueue(
            ScheduleTask(
                run_id=run.id,
                task_id=primary_id,
                worktree_id=resource,
                max_repairs=primary.max_repairs,
            )
        )
        await work.runs.transition(
            run.id,
            run.version,
            RunState.PLANNING,
            _EVENT,
            {
                "command_id": str(command.id),
                "envelope_digest": envelope_digest,
                "resource_id": resource,
                "primary_task": encode_subscription_record(primary),
            },
            actor_class="worker",
        )
        await work.commit()
        return primary_id
