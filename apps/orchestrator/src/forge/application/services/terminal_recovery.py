"""Acknowledge verified terminal merge outcomes without dispatching a command."""

from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.merge_authority import verify_merge_delivery
from forge.application.services.release_resume import resumed_release_origin
from forge.domain.command import CommandStatus
from forge.domain.event import RunEvent
from forge.domain.run import RunState
from forge.persistence.models import Approval
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.release import ReleaseRecordConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork


class TerminalMergeRecovery:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def reconcile_all(self) -> tuple[UUID, ...]:
        acknowledged = []
        for source in await PostgresCommandRepository(
            self._factory
        ).list_expired_terminal_commands():
            if source.command_type != "merge_pr":
                continue
            async with PostgresUnitOfWork(self._factory) as work:
                run = await work.runs.get_for_update(source.run_id)
                current = await work.commands.get(source.id)
                if current.status is CommandStatus.COMPLETED:
                    continue
                if current != source:
                    raise CommandRecoveryRequired("terminal merge delivery changed")
                origin = await resumed_release_origin(cast(UnitOfWork, work), source)
                approval_id = UUID(str(origin.payload["approval_id"]))
                approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
                record = await work.releases.get_for_run(run.id)
                if (
                    run.state is not RunState.COMPLETED
                    or run.version != source.expected_run_version + 1
                    or not isinstance(approval, Approval)
                    or approval.run_id != run.id
                    or approval.gate != "merge"
                    or approval.authenticated_actor_id != source.actor_id
                    or source.actor_id is None
                    or approval.policy_version != run.policy_version
                    or approval.run_version + 1 != origin.expected_run_version
                    or record is None
                    or record.merge_intent_id is None
                ):
                    raise CommandRecoveryRequired("terminal merge authority differs")
                await verify_merge_delivery(source, cast(UnitOfWork, work), approval)
                events = [
                    e
                    for e in await work.events.list_after(run.id, 0)
                    if e.event_type == "run.merge_completed"
                    and e.payload.get("source_command_id") == str(source.id)
                ]
                if (
                    len(events) != 1
                    or events[0].run_version != run.version
                    or events[0].actor_class != "worker"
                    or events[0].actor_id != source.actor_id
                    or events[0].payload_schema_version != 1
                    or events[0].payload
                    != {
                        "source_command_id": str(source.id),
                        "approval_id": str(approval.id),
                        "pull_request_id": str(record.id),
                        "merge_intent_id": str(record.merge_intent_id),
                        "merge_sha": record.pull_request.merge_sha,
                    }
                ):
                    raise CommandRecoveryRequired("terminal merge completion differs")
                try:
                    await work.releases.record_merge(
                        run.id, record.pull_request, record.merge_intent_id
                    )
                except ReleaseRecordConflict:
                    raise CommandRecoveryRequired("terminal merge receipt differs") from None
                intent = await work.operations.get(record.merge_intent_id)
                if intent.request_payload.get("approval_id") != str(approval.id):
                    raise CommandRecoveryRequired("terminal merge receipt approval differs")
                if await work.commands.complete_expired_observed_lease(source) is None:
                    raise CommandRecoveryRequired("terminal merge lease changed")
                await work.events.append(
                    RunEvent(
                        run_id=run.id,
                        run_version=run.version,
                        event_type="merge.acknowledged_on_recovery",
                        actor_class="worker",
                        actor_id=None,
                        payload={
                            "source_command_id": str(source.id),
                            "completion_event_id": str(events[0].event_id),
                            "merge_intent_id": str(record.merge_intent_id),
                        },
                    )
                )
                await work.commit()
                acknowledged.append(source.id)
        return tuple(acknowledged)
