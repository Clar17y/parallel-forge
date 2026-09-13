"""Typed PostgreSQL subscription runtime persistence."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TypeVar
from uuid import UUID

from sqlalchemy import Text, case, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription import DecisionRecord, InvocationTaskOutcome
from forge.application.ports.tool_recovery import (
    VerifiedTerminalEffect,
    terminal_call_digest,
    terminal_intent_digest,
)
from forge.application.services.subscription_broker import _receipt_result
from forge.application.services.tool_recovery import ToolRecoveryService
from forge.application.services.tools import ToolInvocationError, _result_from_record
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    CANDIDATE_READ_TOOLS,
    SPECIALIST_ALLOWED_TOOLS,
    AcceptDecision,
    AttemptIdentity,
    BoundReassignDecision,
    BoundScopeResponseDecision,
    BudgetPool,
    DelegateDecision,
    ExecutionEnvelope,
    LogicalTaskContract,
    OperatorProfile,
    ReassignDecision,
    ReviewedTaskHandoff,
    ReviewSelection,
    RouteBinding,
    ScopeRequestDecision,
    ScopeResponseDecision,
    TaskBudget,
    TaskHandoff,
    ToolCallBinding,
    WaitDecision,
    decode_subscription_record,
    encode_subscription_record,
    validate_task_dag,
)
from forge.domain.subscription_execution import run_allows_subscription_attempt
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.domain.tool import (
    SubscriptionToolAuthorizationContext,
    ToolCallStatus,
    ToolError,
    ToolErrorCode,
    ToolName,
    ToolRequest,
    ToolResult,
)
from forge.persistence.models.execution import OperationIntent, ToolCall
from forge.persistence.models.project import Project
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    ProjectSubscriptionProfile,
    SubscriptionAttempt,
    SubscriptionBudgetPool,
    SubscriptionBudgetReservation,
    SubscriptionClientLaunch,
    SubscriptionDecisionRecord,
    SubscriptionEnvelope,
    SubscriptionOperationBinding,
    SubscriptionProfileVersion,
    SubscriptionTask,
    SubscriptionTaskDependency,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.repositories.projects import ProjectNotFound
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.scheduling import PostgresSchedulingRepository
from forge.persistence.repositories.tool_calls import PostgresToolCallRepository, _record_from_row


class SubscriptionConflict(ValueError):
    pass


class SubscriptionProfileNotFound(SubscriptionConflict):
    pass


_RECORDS = (
    BoundReassignDecision,
    BoundScopeResponseDecision,
    TaskHandoff,
    ReviewedTaskHandoff,
    DelegateDecision,
    WaitDecision,
    ScopeRequestDecision,
    ScopeResponseDecision,
    AcceptDecision,
    ReassignDecision,
    ReviewSelection,
)

T = TypeVar("T")
D = TypeVar("D", bound=DecisionRecord)


class PostgresSubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def interrupted_effect_ids(self, after_id: UUID | None, limit: int) -> tuple[UUID, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("recovery page limit must be 1..100")
        query = select(SubscriptionScheduledEffect.id).where(
            SubscriptionScheduledEffect.state.in_(("admitted", "reconciling"))
        )
        if after_id is not None:
            query = query.where(SubscriptionScheduledEffect.id > after_id)
        return tuple(
            (
                await self._session.scalars(
                    query.order_by(SubscriptionScheduledEffect.id).limit(limit)
                )
            ).all()
        )

    async def reconcile_interrupted_effect(
        self, effect_id: UUID, *, verified_terminal: VerifiedTerminalEffect | None = None
    ) -> bool:
        """Reject proved terminal effects whose broker finalization was interrupted.

        This deliberately uses historical immutable evidence.  Calling
        ``authorize_tool`` here would incorrectly require the lease that was
        revoked precisely because the callback was interrupted.
        """
        return await self._reconcile_interrupted_effect(effect_id, verified_terminal)

    async def _reconcile_interrupted_effect(
        self, effect_id: UUID, verified_terminal: VerifiedTerminalEffect | None
    ) -> bool:
        # Discover the run before taking locks, then take the same durable
        # order as broker finalization: run -> task/attempt/binding ->
        # scheduled task -> effect.  Every value is rechecked after locking.
        discovered = await self._session.get(SubscriptionScheduledEffect, effect_id)
        if discovered is None:
            return False
        await self._lock_run(discovered.run_id)
        envelope = await self._session.get(
            SubscriptionEnvelope, discovered.run_id, with_for_update=True
        )
        if envelope is None:
            return False
        task = await self._task(discovered.run_id, discovered.task_id, True)
        attempts = (
            (
                await self._session.execute(
                    select(SubscriptionAttempt)
                    .where(
                        SubscriptionAttempt.run_id == discovered.run_id,
                        SubscriptionAttempt.task_row_id == discovered.task_id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if not attempts:
            return False
        bindings = (
            (
                await self._session.execute(
                    select(SubscriptionOperationBinding)
                    .where(
                        SubscriptionOperationBinding.attempt_id.in_(
                            [attempt.id for attempt in attempts]
                        ),
                        SubscriptionOperationBinding.durable_operation_id == effect_id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        # durable_operation_id is intentionally not unique: ambiguity must
        # remain fenced rather than selecting an arbitrary provider callback.
        if len(bindings) != 1:
            return False
        binding_row = bindings[0]
        attempt = next((value for value in attempts if value.id == binding_row.attempt_id), None)
        if attempt is None:
            return False
        try:
            binding = self._decode(binding_row.payload, ToolCallBinding)
            contract = self._decode(task.payload, LogicalTaskContract)
            frozen = self._decode(envelope.payload, ExecutionEnvelope)
        except ToolInvocationError, TypeError, ValueError:
            return False
        if (
            binding.attempt_id != attempt.id
            or binding.durable_operation_id != effect_id
            or binding.provider_call_key != binding_row.provider_call_key
        ):
            return False
        scheduled = (
            await self._session.execute(
                select(SubscriptionScheduledTask)
                .where(
                    SubscriptionScheduledTask.run_id == discovered.run_id,
                    SubscriptionScheduledTask.task_id == discovered.task_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        effect = await self._session.get(
            SubscriptionScheduledEffect, effect_id, with_for_update=True
        )
        if (
            scheduled is None
            or effect is None
            or effect.state not in {"admitted", "reconciling"}
            or effect.run_id != discovered.run_id
            or effect.task_id != discovered.task_id
        ):
            return False
        now = datetime.now(UTC)
        current_lease = (
            scheduled.state == "leased"
            and not scheduled.pause_requested
            and not scheduled.cancel_requested
            and scheduled.lease_owner == effect.lease_owner
            and scheduled.lease_generation == effect.lease_generation
            and scheduled.lease_expires_at is not None
            and scheduled.lease_expires_at > now
        )
        # A live admitted callback can still commit its own receipt.  Only a
        # task already reconciling, revoked, expired, nonleased, or superseded
        # generation is safe to force into the denied terminal outcome.
        if current_lease:
            return False
        terminal_row = (
            await self._session.execute(
                select(ToolCall).where(ToolCall.id == effect_id).with_for_update()
            )
        ).scalar_one_or_none()
        if terminal_row is None:
            return False
        try:
            terminal = _record_from_row(terminal_row)
            historical = _result_from_record(terminal)
        except ToolInvocationError, TypeError, ValueError:
            return False
        if (
            terminal.status
            not in {
                ToolCallStatus.SUCCEEDED,
                ToolCallStatus.FAILED,
                ToolCallStatus.DENIED,
                ToolCallStatus.CANCELLED,
            }
            or terminal.completed_at is None
            or terminal.agent_execution_id is not None
            or terminal.run_id != effect.run_id
            or terminal.subscription_task_id != effect.task_id
            or terminal.subscription_attempt_id != attempt.id
            or terminal.subscription_purpose != contract.purpose.value
            or terminal.tool_name is not binding.tool_name
            or terminal.correlation_id != effect_id
            or terminal.policy_version != frozen.safety_policy_version
            or terminal.request_digest is None
            or terminal.resource_id != scheduled.worktree_id
            or terminal.invocation_schema_version != 1
            or historical.tool_name is not binding.tool_name
        ):
            return False
        effectful = binding.tool_name in {
            ToolName.REPOSITORY_WRITE_FILE,
            ToolName.REPOSITORY_DELETE_FILE,
            ToolName.REPOSITORY_RENAME_FILE,
        }
        if effectful:
            if terminal.request_digest != binding.arguments_digest:
                return False
            operation = await self._session.get(OperationIntent, effect_id, with_for_update=True)
            if terminal.status is ToolCallStatus.DENIED and not terminal.authorized:
                admitted_intent = await self._session.scalar(
                    select(OperationIntent.id).where(
                        OperationIntent.idempotency_key == f"tool:{effect_id}"
                    )
                )
                if (
                    terminal.operation_intent_id is not None
                    or operation is not None
                    or admitted_intent is not None
                ):
                    return False
            else:
                if not terminal.authorized:
                    return False
                authority = {
                    "authority_schema_version": 2,
                    "subscription_task_id": str(effect.task_id),
                    "subscription_attempt_id": str(attempt.id),
                    "subscription_purpose": contract.purpose.value,
                    "run_id": str(effect.run_id),
                    "policy_version": frozen.safety_policy_version,
                    "request_digest": terminal.request_digest,
                    "worktree_id": scheduled.worktree_id,
                }
                if (
                    operation is None
                    or terminal.operation_intent_id != effect_id
                    or operation.run_id != effect.run_id
                    or operation.operation_kind != binding.tool_name.value
                    or operation.idempotency_key != f"tool:{effect_id}"
                    or operation.request_schema_version != 1
                    or operation.request_digest != canonical_digest(operation.request_payload)
                    or operation.status != "SUCCEEDED"
                    or operation.completed_at is None
                    or any(
                        operation.request_payload.get(key) != value
                        for key, value in authority.items()
                    )
                ):
                    return False
                try:
                    intent = await PostgresOperationRepository(session=self._session).get(effect_id)
                    run = await PostgresRunRepository(self._session).get(effect.run_id)
                    outcome = intent.outcome_payload
                    if outcome is None or not ToolRecoveryService.valid_repository_mutation(
                        terminal, intent, run, outcome
                    ):
                        return False
                except ToolInvocationError, TypeError, ValueError:
                    return False
        elif binding.tool_name in {ToolName.BUILD_RUN_NAMED_CHECK, ToolName.GIT_COMMIT}:
            if (
                verified_terminal is None
                or verified_terminal.effect_id != effect_id
                or verified_terminal.call_digest != terminal_call_digest(terminal)
                or terminal.operation_intent_id != effect_id
                or terminal.request_digest != binding.arguments_digest
                or not effect.whole_worktree_exclusive
            ):
                return False
            # Pin the proved operation rows until receipt/effect settlement commits.
            locked = (
                await self._session.scalars(
                    select(OperationIntent)
                    .where(
                        OperationIntent.id.in_(
                            [identity for identity, _ in verified_terminal.intent_digests]
                        )
                    )
                    .order_by(OperationIntent.id)
                    .with_for_update()
                )
            ).all()
            if len(locked) != len(verified_terminal.intent_digests):
                return False
            operations = PostgresOperationRepository(session=self._session)
            intent = await operations.get(effect_id)
            intents = [intent]
            if binding.tool_name is ToolName.GIT_COMMIT:
                publication = await operations.get_by_idempotency_key(
                    f"git.commit:{effect_id}:publish"
                )
                if publication is None:
                    return False
                intents.append(publication)
            if (
                tuple((item.id, terminal_intent_digest(item)) for item in intents)
                != verified_terminal.intent_digests
            ):
                return False
        elif (
            terminal.operation_intent_id is not None
            or canonical_digest(dict(terminal.normalized_arguments)) != binding.arguments_digest
        ):
            return False
        if binding_row.receipt_payload is not None:
            return False
        denied = ToolResult(
            tool_name=binding.tool_name,
            status=ToolCallStatus.DENIED,
            error=ToolError(
                code=ToolErrorCode.AUTHORIZATION_DENIED,
                message="tool result acceptance was revoked",
            ),
        )
        binding_row.receipt_payload = {"accepted": False, "result": _receipt_result(denied)}
        effect.state = "rejected"
        await self._session.flush()
        return True

    @staticmethod
    def _decode(payload: Mapping[str, object], expected: type[T]) -> T:
        value = decode_subscription_record(payload)
        if not isinstance(value, expected):
            raise SubscriptionConflict("stored record type mismatch")
        return value

    async def authorize_tool(
        self, context: SubscriptionToolAuthorizationContext, request: ToolRequest
    ) -> LogicalTaskContract | None:
        """Prove a subscription callback's complete durable admission lineage.

        This takes only short row locks; it does not execute an effect or
        retain the transaction across a provider callback.
        """
        if (
            context.invocation_id is None
            or context.operation_intent_id is None
            or context.invocation_id != context.operation_intent_id
        ):
            return None
        # Serialize with attempt creation and tool budget admission. Scheduler
        # task precedes scheduler run, matching admit_effect's lock order.
        await self._lock_run(context.run_id)
        envelope = await self._session.get(
            SubscriptionEnvelope, context.run_id, with_for_update=True
        )
        task = (
            await self._session.execute(
                select(SubscriptionTask)
                .where(
                    SubscriptionTask.run_id == context.run_id,
                    SubscriptionTask.id == context.task_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            envelope is None
            or task is None
            or task.state != "running"
            or task.pause_requested
            or task.cancel_requested
        ):
            return None
        try:
            contract = self._decode(task.payload, LogicalTaskContract)
            frozen = self._decode(envelope.payload, ExecutionEnvelope)
        except TypeError, ValueError:
            return None
        if (
            contract.purpose is not context.purpose
            or frozen.safety_policy_version != context.policy_version
            or request.name not in context.permitted_tools
            or not context.permitted_tools <= SPECIALIST_ALLOWED_TOOLS[contract.purpose]
        ):
            return None
        attempt = await self._session.get(
            SubscriptionAttempt, context.attempt_id, with_for_update=True
        )
        if (
            attempt is None
            or attempt.run_id != context.run_id
            or attempt.task_row_id != task.id
            or attempt.status != "running"
        ):
            return None
        latest = await self._session.scalar(
            select(func.max(SubscriptionAttempt.attempt_number)).where(
                SubscriptionAttempt.run_id == context.run_id,
                SubscriptionAttempt.task_row_id == context.task_id,
            )
        )
        if latest != attempt.attempt_number:
            return None
        try:
            attempt_route = self._decode(attempt.route_payload, RouteBinding)
            approved_route = frozen.route_for(contract.purpose)
        except KeyError, TypeError, ValueError:
            return None
        if (
            contract.route.requested != approved_route.requested
            or attempt_route.requested != contract.route.requested
        ):
            return None
        if attempt_route != approved_route and (
            attempt_route.effective not in frozen.fallbacks_for(contract.purpose)
        ):
            return None
        binding = (
            await self._session.execute(
                select(SubscriptionOperationBinding)
                .where(
                    SubscriptionOperationBinding.attempt_id == context.attempt_id,
                    SubscriptionOperationBinding.durable_operation_id
                    == context.operation_intent_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if binding is None:
            return None
        try:
            stored = self._decode(binding.payload, ToolCallBinding)
        except TypeError, ValueError:
            return None
        if (
            stored.attempt_id != context.attempt_id
            or stored.durable_operation_id != context.operation_intent_id
            or stored.provider_call_key != binding.provider_call_key
            or stored.tool_name is not request.name
            or stored.arguments_digest != canonical_digest(dict(request.arguments))
        ):
            return None
        scheduled = (
            await self._session.execute(
                select(SubscriptionScheduledTask)
                .where(
                    SubscriptionScheduledTask.run_id == context.run_id,
                    SubscriptionScheduledTask.task_id == context.task_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        scheduler_run = await self._session.get(
            SubscriptionSchedulerRun, context.run_id, with_for_update=True
        )
        effect = await self._session.get(
            SubscriptionScheduledEffect, context.operation_intent_id, with_for_update=True
        )
        if (
            scheduler_run is None
            or scheduled is None
            or effect is None
            or not scheduler_run.admitted
            or not (
                scheduler_run.candidate_state == "open"
                or (
                    scheduler_run.candidate_state == "closed"
                    and request.name in CANDIDATE_READ_TOOLS
                    and await PostgresSchedulingRepository(self._session)._can_read_candidate(
                        scheduled
                    )
                )
            )
            or scheduled.state != "leased"
            or scheduled.worktree_id != context.worktree_id
            or scheduled.pause_requested
            or scheduled.cancel_requested
            or scheduled.lease_owner is None
            or scheduled.lease_expires_at is None
            or scheduled.lease_expires_at <= datetime.now(UTC)
            or effect.run_id != context.run_id
            or effect.task_id != context.task_id
            or effect.lease_owner != scheduled.lease_owner
            or effect.lease_generation != scheduled.lease_generation
            or effect.candidate_epoch != scheduler_run.candidate_epoch
            or effect.state not in {"admitted", "settled", "rejected"}
        ):
            return None
        if effect.state != "admitted":
            terminal = await PostgresToolCallRepository(self._session).find(context.invocation_id)
            if (
                terminal is None
                or terminal.status
                not in {
                    ToolCallStatus.SUCCEEDED,
                    ToolCallStatus.FAILED,
                    ToolCallStatus.DENIED,
                    ToolCallStatus.CANCELLED,
                }
                or terminal.run_id != context.run_id
                or terminal.subscription_task_id != context.task_id
                or terminal.subscription_attempt_id != context.attempt_id
                or terminal.subscription_purpose != context.purpose.value
                or terminal.tool_name is not request.name
                or terminal.operation_intent_id != context.operation_intent_id
                or terminal.policy_version != context.policy_version
                or terminal.resource_id != context.worktree_id
            ):
                return None
        return contract

    async def _lock_run(self, run_id: UUID) -> None:
        row = (
            await self._session.execute(select(Run.id).where(Run.id == run_id).with_for_update())
        ).scalar_one_or_none()
        if row is None:
            raise SubscriptionConflict("run not found")

    async def _lock_project(self, project_id: UUID) -> None:
        row = (
            await self._session.execute(
                select(Project.id).where(Project.id == project_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise ProjectNotFound("project not found")

    async def store_profile(self, profile: OperatorProfile) -> OperatorProfile:
        payload = encode_subscription_record(profile)
        stmt = (
            insert(SubscriptionProfileVersion)
            .values(
                profile_id=profile.profile_id,
                version=profile.version,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=["profile_id", "version"])
        )
        await self._session.execute(stmt)
        row = (
            await self._session.execute(
                select(SubscriptionProfileVersion).where(
                    SubscriptionProfileVersion.profile_id == profile.profile_id,
                    SubscriptionProfileVersion.version == profile.version,
                )
            )
        ).scalar_one()
        if row.payload != payload:
            raise SubscriptionConflict("profile version is immutable")
        return self._decode(row.payload, OperatorProfile)

    async def list_profiles(self) -> list[OperatorProfile]:
        rows = (
            (
                await self._session.execute(
                    select(SubscriptionProfileVersion).order_by(
                        SubscriptionProfileVersion.profile_id, SubscriptionProfileVersion.version
                    )
                )
            )
            .scalars()
            .all()
        )
        return [self._decode(row.payload, OperatorProfile) for row in rows]

    async def profile(self, profile_id: UUID, version: int) -> OperatorProfile:
        row = (
            await self._session.execute(
                select(SubscriptionProfileVersion).where(
                    SubscriptionProfileVersion.profile_id == profile_id,
                    SubscriptionProfileVersion.version == version,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise SubscriptionProfileNotFound("profile version not found")
        return self._decode(row.payload, OperatorProfile)

    async def append_profile(
        self, profile: OperatorProfile, *, expected_current_version: int
    ) -> OperatorProfile:
        if expected_current_version < 1 or profile.version != expected_current_version + 1:
            raise SubscriptionConflict("expected profile version is stale")
        # Profiles have no mutable parent row.  A transaction-scoped advisory lock
        # makes max(version)+1 and the immutable insert one serialization point.
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(str(profile.profile_id), 0)))
        )
        current = (
            await self._session.execute(
                select(func.max(SubscriptionProfileVersion.version)).where(
                    SubscriptionProfileVersion.profile_id == profile.profile_id
                )
            )
        ).scalar_one()
        if current != expected_current_version:
            raise SubscriptionConflict("expected profile version is stale")
        return await self.store_profile(profile)

    async def project_profile(self, project_id: UUID) -> OperatorProfile | None:
        row = await self._session.get(ProjectSubscriptionProfile, project_id)
        if row is None:
            return None
        return await self.profile(row.profile_id, row.profile_version)

    async def select_project_profile_expected(
        self,
        project_id: UUID,
        profile: OperatorProfile,
        *,
        expected_profile_id: UUID | None,
        expected_profile_version: int | None,
    ) -> None:
        if (expected_profile_id is None) != (expected_profile_version is None):
            raise SubscriptionConflict("expected profile identity is incomplete")
        await self._lock_project(project_id)
        persisted = await self.profile(profile.profile_id, profile.version)
        if persisted != profile:
            raise SubscriptionConflict("profile version is immutable")
        row = await self._session.get(ProjectSubscriptionProfile, project_id, with_for_update=True)
        actual_id = None if row is None else row.profile_id
        actual_version = None if row is None else row.profile_version
        if (actual_id, actual_version) != (expected_profile_id, expected_profile_version):
            raise SubscriptionConflict("expected selected profile is stale")
        if row is None:
            self._session.add(
                ProjectSubscriptionProfile(
                    project_id=project_id,
                    profile_id=profile.profile_id,
                    profile_version=profile.version,
                )
            )
        else:
            row.profile_id, row.profile_version = profile.profile_id, profile.version
        await self._session.flush()

    async def select_project_profile(self, project_id: UUID, profile: OperatorProfile) -> None:
        await self._lock_project(project_id)
        await self.store_profile(profile)
        row = await self._session.get(ProjectSubscriptionProfile, project_id, with_for_update=True)
        if row is None:
            self._session.add(
                ProjectSubscriptionProfile(
                    project_id=project_id,
                    profile_id=profile.profile_id,
                    profile_version=profile.version,
                )
            )
        else:
            row.profile_id, row.profile_version = profile.profile_id, profile.version
        await self._session.flush()

    async def freeze_envelope(self, envelope: ExecutionEnvelope) -> ExecutionEnvelope:
        await self._lock_run(envelope.run_id)
        payload = encode_subscription_record(envelope)
        row = await self._session.get(SubscriptionEnvelope, envelope.run_id, with_for_update=True)
        if row is None:
            self._session.add(
                SubscriptionEnvelope(
                    run_id=envelope.run_id,
                    profile_id=envelope.profile_id,
                    profile_version=envelope.profile_version,
                    safety_policy_version=envelope.safety_policy_version,
                    payload=payload,
                )
            )
            await self._session.flush()
            return envelope
        if row.payload != payload:
            raise SubscriptionConflict("run execution envelope is frozen")
        return self._decode(row.payload, ExecutionEnvelope)

    async def envelope_for_run(self, run_id: UUID) -> ExecutionEnvelope | None:
        row = await self._session.get(SubscriptionEnvelope, run_id)
        return None if row is None else self._decode(row.payload, ExecutionEnvelope)

    async def create_task(
        self, contract: LogicalTaskContract, *, idempotency_key: str
    ) -> LogicalTaskContract:
        await self._lock_run(contract.run_id)
        payload = encode_subscription_record(contract)
        row = (
            await self._session.execute(
                select(SubscriptionTask)
                .where(
                    SubscriptionTask.run_id == contract.run_id,
                    SubscriptionTask.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is not None:
            if row.payload != payload:
                raise SubscriptionConflict("task replay conflicts")
            return self._decode(row.payload, LogicalTaskContract)

        # Prospective DAG validation under short run lock
        existing_rows = (
            (
                await self._session.execute(
                    select(SubscriptionTask.payload)
                    .where(SubscriptionTask.run_id == contract.run_id)
                    .order_by(SubscriptionTask.created_at)
                )
            )
            .scalars()
            .all()
        )
        existing_tasks = [self._decode(r, LogicalTaskContract) for r in existing_rows]
        prospective_tasks = [*existing_tasks, contract]
        try:
            validate_task_dag(prospective_tasks)
        except (ValueError, TypeError) as exc:
            msg = str(exc)
            if (
                "foreign lineage" in msg
                or "mismatched run" in msg
                or "does not exist in run" in msg
            ):
                raise SubscriptionConflict("foreign task lineage") from exc
            raise SubscriptionConflict(msg) from exc

        self._session.add(
            SubscriptionTask(
                id=contract.task_id,
                run_id=contract.run_id,
                task_id=contract.task_id,
                parent_task_id=contract.parent_task_id,
                idempotency_key=idempotency_key,
                payload=payload,
            )
        )
        await self._session.flush()
        self._session.add_all(
            SubscriptionTaskDependency(
                run_id=contract.run_id,
                task_id=contract.task_id,
                dependency_task_id=x,
            )
            for x in contract.dependency_task_ids
        )
        await self._session.flush()
        return contract

    async def create_attempt(
        self,
        attempt: AttemptIdentity,
        *,
        route_payload: RouteBinding,
        idempotency_key: str,
    ) -> AttemptIdentity:
        await self._lock_run(attempt.run_id)
        task = await self._task(attempt.run_id, attempt.task_id, True)
        if not isinstance(route_payload, RouteBinding):
            raise TypeError("route_payload must be RouteBinding")
        binding = route_payload

        task_contract = self._decode(task.payload, LogicalTaskContract)
        if binding.requested != task_contract.route.requested:
            raise SubscriptionConflict("attempt route does not align with task request")
        if binding.effective == task_contract.route.effective and binding != task_contract.route:
            raise SubscriptionConflict("attempt route does not align with task binding")
        if binding.effective != task_contract.route.effective:
            envelope = await self.envelope_for_run(attempt.run_id)
            if envelope is None or binding.effective not in envelope.fallbacks_for(
                task_contract.purpose
            ):
                raise SubscriptionConflict("attempt route does not align with task or envelope")

        payload = encode_subscription_record(binding)
        row = (
            await self._session.execute(
                select(SubscriptionAttempt)
                .where(
                    SubscriptionAttempt.task_row_id == task.id,
                    SubscriptionAttempt.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is not None:
            if (
                row.id != attempt.attempt_id
                or row.attempt_number != attempt.attempt_number
                or row.route_payload != payload
            ):
                raise SubscriptionConflict("attempt replay conflicts")
            return attempt
        self._session.add(
            SubscriptionAttempt(
                id=attempt.attempt_id,
                run_id=attempt.run_id,
                task_row_id=task.id,
                attempt_number=attempt.attempt_number,
                idempotency_key=idempotency_key,
                route_payload=payload,
            )
        )
        await self._session.flush()
        return attempt

    async def bind_operation(
        self, binding: ToolCallBinding, *, run_id: UUID, task_id: UUID
    ) -> ToolCallBinding:
        await self._lock_run(run_id)
        task = await self._task(run_id, task_id, True)
        attempt = await self._session.get(
            SubscriptionAttempt, binding.attempt_id, with_for_update=True
        )
        if attempt is None:
            raise SubscriptionConflict("unknown attempt")
        if attempt.task_row_id != task.id or attempt.run_id != run_id:
            raise SubscriptionConflict("foreign attempt lineage")
        payload = encode_subscription_record(binding)
        row = (
            await self._session.execute(
                select(SubscriptionOperationBinding)
                .where(
                    SubscriptionOperationBinding.attempt_id == binding.attempt_id,
                    SubscriptionOperationBinding.provider_call_key == binding.provider_call_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            self._session.add(
                SubscriptionOperationBinding(
                    attempt_id=binding.attempt_id,
                    provider_call_key=binding.provider_call_key,
                    durable_operation_id=binding.durable_operation_id,
                    payload=payload,
                )
            )
            await self._session.flush()
            return binding
        if row.payload != payload:
            raise SubscriptionConflict("operation replay conflicts")
        return self._decode(row.payload, ToolCallBinding)

    async def record_operation_receipt(
        self,
        binding: ToolCallBinding,
        *,
        run_id: UUID,
        task_id: UUID,
        receipt: Mapping[str, object],
    ) -> ToolCallBinding:
        """Set an immutable receipt after verifying the complete operation lineage."""
        await self.bind_operation(binding, run_id=run_id, task_id=task_id)
        row = (
            await self._session.execute(
                select(SubscriptionOperationBinding)
                .where(
                    SubscriptionOperationBinding.attempt_id == binding.attempt_id,
                    SubscriptionOperationBinding.provider_call_key == binding.provider_call_key,
                )
                .with_for_update()
            )
        ).scalar_one()
        payload = dict(receipt)
        if row.receipt_payload is None:
            row.receipt_payload = payload
            await self._session.flush()
        elif row.receipt_payload != payload:
            raise SubscriptionConflict("operation receipt conflicts")
        return self._decode(row.payload, ToolCallBinding)

    async def operation_receipt(
        self, binding: ToolCallBinding, *, run_id: UUID, task_id: UUID
    ) -> Mapping[str, object] | None:
        await self.bind_operation(binding, run_id=run_id, task_id=task_id)
        row = (
            await self._session.execute(
                select(SubscriptionOperationBinding).where(
                    SubscriptionOperationBinding.attempt_id == binding.attempt_id,
                    SubscriptionOperationBinding.provider_call_key == binding.provider_call_key,
                )
            )
        ).scalar_one()
        return None if row.receipt_payload is None else dict(row.receipt_payload)

    async def operation_evidence(
        self, operation_id: UUID, *, run_id: UUID, task_id: UUID, attempt_id: UUID
    ) -> tuple[ToolCallBinding, Mapping[str, object]] | None:
        """Read existing callback evidence without reserving or recreating a binding."""
        rows = (
            await self._session.scalars(
                select(SubscriptionOperationBinding)
                .join(
                    SubscriptionAttempt,
                    SubscriptionAttempt.id == SubscriptionOperationBinding.attempt_id,
                )
                .join(SubscriptionTask, SubscriptionTask.id == SubscriptionAttempt.task_row_id)
                .where(
                    SubscriptionOperationBinding.durable_operation_id == operation_id,
                    SubscriptionOperationBinding.attempt_id == attempt_id,
                    SubscriptionTask.id == task_id,
                    SubscriptionTask.run_id == run_id,
                )
                .limit(2)
            )
        ).all()
        if len(rows) != 1:
            return None
        row = rows[0]
        if row.receipt_payload is None:
            return None
        binding = self._decode(row.payload, ToolCallBinding)
        if binding.attempt_id != attempt_id or binding.durable_operation_id != operation_id:
            raise SubscriptionConflict("operation evidence lineage differs")
        return binding, dict(row.receipt_payload)

    async def launch_intent(
        self, attempt_id: UUID, launch_id: str, *, worker_identity: str
    ) -> None:
        if not launch_id or not worker_identity:
            raise SubscriptionConflict("launch identity is invalid")
        run_id = await self._session.scalar(
            select(SubscriptionAttempt.run_id).where(SubscriptionAttempt.id == attempt_id)
        )
        if run_id is None:
            raise SubscriptionConflict("unknown attempt")
        # Match admission/settlement lock order; lifecycle updates must not hold
        # an attempt lock while waiting for an operator's run control lock.
        run = await self._session.get(Run, run_id, with_for_update=True, populate_existing=True)
        attempt = await self._session.get(
            SubscriptionAttempt, attempt_id, with_for_update=True, populate_existing=True
        )
        if attempt is None:
            raise SubscriptionConflict("unknown attempt")
        row = (
            await self._session.execute(
                select(SubscriptionClientLaunch)
                .where(
                    SubscriptionClientLaunch.attempt_id == attempt_id,
                    SubscriptionClientLaunch.launch_id == launch_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            if attempt.status != "running" or attempt.lease_owner != worker_identity:
                raise SubscriptionConflict("launch is not owned by a running attempt")
            task = await self._session.get(
                SubscriptionTask, attempt.task_row_id, with_for_update=True, populate_existing=True
            )
            scheduled = await self._session.scalar(
                select(SubscriptionScheduledTask)
                .where(SubscriptionScheduledTask.task_id == attempt.task_row_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                run is None
                or not run_allows_subscription_attempt(run.state, run.pending_gate)
                or task is None
                or task.pause_requested
                or task.cancel_requested
                or scheduled is None
                or scheduled.state != "leased"
                or scheduled.pause_requested
                or scheduled.cancel_requested
                or scheduled.lease_owner != worker_identity
                or scheduled.lease_generation != attempt.lease_generation
                or scheduled.lease_expires_at is None
                or scheduled.lease_expires_at <= datetime.now(UTC)
            ):
                raise SubscriptionConflict("launch admission is stopped or expired")
            self._session.add(
                SubscriptionClientLaunch(
                    attempt_id=attempt_id, launch_id=launch_id, worker_identity=worker_identity
                )
            )
            await self._session.flush()
        elif row.worker_identity != worker_identity:
            raise SubscriptionConflict("launch identity conflicts")

    async def launch_started(
        self,
        attempt_id: UUID,
        launch_id: str,
        *,
        worker_identity: str,
        pid: int,
        process_start_token: str,
    ) -> None:
        if pid <= 0 or not process_start_token:
            raise SubscriptionConflict("started process identity is invalid")
        await self.launch_intent(attempt_id, launch_id, worker_identity=worker_identity)
        row = (
            await self._session.execute(
                select(SubscriptionClientLaunch)
                .where(
                    SubscriptionClientLaunch.attempt_id == attempt_id,
                    SubscriptionClientLaunch.launch_id == launch_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        if row.state == "intent":
            row.state, row.pid, row.process_start_token = "started", pid, process_start_token
            await self._session.flush()
        elif (row.worker_identity, row.pid, row.process_start_token) != (
            worker_identity,
            pid,
            process_start_token,
        ):
            raise SubscriptionConflict("started process identity conflicts")

    async def launch_finished(
        self,
        attempt_id: UUID,
        launch_id: str,
        *,
        worker_identity: str,
        terminal: SubscriptionLaunchTerminalProof | None,
        uncertain: bool,
    ) -> None:
        await self.launch_intent(attempt_id, launch_id, worker_identity=worker_identity)
        row = (
            await self._session.execute(
                select(SubscriptionClientLaunch)
                .where(
                    SubscriptionClientLaunch.attempt_id == attempt_id,
                    SubscriptionClientLaunch.launch_id == launch_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        if terminal is not None and not isinstance(terminal, SubscriptionLaunchTerminalProof):
            raise SubscriptionConflict("terminal launch proof must be typed")
        if terminal is not None and (row.launch_id, row.pid, row.process_start_token) != (
            terminal.launch_id,
            terminal.pid,
            terminal.process_identity,
        ):
            raise SubscriptionConflict("terminal launch process identity differs")
        if not uncertain and (terminal is None or not terminal.stop_confirmed):
            raise SubscriptionConflict("terminal launch stop is unproven")
        payload = None if terminal is None else terminal.model_dump(mode="json")
        state = "uncertain" if uncertain else "terminal"
        if row.state in {"terminal", "uncertain"}:
            if row.state != state or row.terminal_payload != payload:
                raise SubscriptionConflict("launch terminal receipt conflicts")
            return
        row.state, row.terminal_payload = state, payload
        await self._session.flush()

    async def initialize_budget(
        self, run_id: UUID, task_id: UUID | None, total: TaskBudget
    ) -> BudgetPool:
        await self._lock_run(run_id)
        task = await self._task_or_none(run_id, task_id, True)
        row = await self._pool(run_id, None if task is None else task.id, True)
        initial = BudgetPool(total_budget=total)
        if row is None:
            self._session.add(
                SubscriptionBudgetPool(
                    run_id=run_id,
                    task_row_id=None if task is None else task.id,
                    payload=encode_subscription_record(initial),
                )
            )
            await self._session.flush()
            return initial
        existing = self._decode(row.payload, BudgetPool)
        if existing.total_budget != total:
            raise SubscriptionConflict("budget pool is immutable")
        return existing

    async def reserve_budget(
        self,
        run_id: UUID,
        task_id: UUID,
        budget: TaskBudget,
        *,
        reservation_id: UUID,
        idempotency_key: str,
    ) -> BudgetPool:
        await self._lock_run(run_id)
        task = await self._task(run_id, task_id, True)
        payload = encode_subscription_record(budget)
        existing = (
            await self._session.execute(
                select(SubscriptionBudgetReservation)
                .where(
                    SubscriptionBudgetReservation.run_id == run_id,
                    SubscriptionBudgetReservation.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (
                existing.id != reservation_id
                or existing.task_row_id != task.id
                or existing.budget_payload != payload
            ):
                raise SubscriptionConflict("budget reservation replay conflicts")
            row = await self._pool(run_id, task.id, True)
            if row is None:
                raise SubscriptionConflict("task budget pool is not initialized")
            return self._decode(row.payload, BudgetPool)
        run_row = await self._pool(run_id, None, True)
        row = await self._pool(run_id, task.id, True)
        if row is None or run_row is None:
            raise SubscriptionConflict("run and task budget pools must be initialized")
        try:
            updated = self._decode(row.payload, BudgetPool).reserve(budget)
            run_updated = self._decode(run_row.payload, BudgetPool).reserve(budget)
        except ValueError as exc:
            raise SubscriptionConflict("invalid budget reservation") from exc
        row.payload = encode_subscription_record(updated)
        run_row.payload = encode_subscription_record(run_updated)
        self._session.add(
            SubscriptionBudgetReservation(
                id=reservation_id,
                run_id=run_id,
                task_row_id=task.id,
                idempotency_key=idempotency_key,
                budget_payload=payload,
            )
        )
        await self._session.flush()
        return updated

    async def settle_budget(
        self,
        run_id: UUID,
        task_id: UUID,
        budget: TaskBudget,
        *,
        reservation_id: UUID,
        consumed: bool,
    ) -> BudgetPool:
        await self._lock_run(run_id)
        task = await self._task(run_id, task_id, True)
        reservation = await self._session.get(
            SubscriptionBudgetReservation, reservation_id, with_for_update=True
        )
        payload = encode_subscription_record(budget)
        if (
            reservation is None
            or reservation.run_id != run_id
            or reservation.task_row_id != task.id
            or reservation.budget_payload != payload
        ):
            raise SubscriptionConflict("foreign or conflicting budget reservation")
        target = "consumed" if consumed else "released"
        if reservation.status != "reserved":
            if reservation.status != target:
                raise SubscriptionConflict("budget settlement conflicts")
            row = await self._pool(run_id, task.id, True)
            if row is None:
                raise SubscriptionConflict("task budget pool is not initialized")
            return self._decode(row.payload, BudgetPool)
        row = await self._pool(run_id, task.id, True)
        run_row = await self._pool(run_id, None, True)
        if row is None or run_row is None:
            raise SubscriptionConflict("run and task budget pools must be initialized")
        if not consumed:
            try:
                row.payload = encode_subscription_record(
                    self._decode(row.payload, BudgetPool).release(budget)
                )
                run_row.payload = encode_subscription_record(
                    self._decode(run_row.payload, BudgetPool).release(budget)
                )
            except ValueError as exc:
                raise SubscriptionConflict("invalid budget settlement") from exc
        reservation.status = target
        await self._session.flush()
        return self._decode(row.payload, BudgetPool)

    async def record_decision(self, record: D, *, idempotency_key: str) -> D:
        if type(record) not in _RECORDS:
            raise TypeError("typed decision or handoff required")
        await self._lock_run(record.run_id)
        # Targeted primary decisions name their subject, not their producer.
        # Application attaches the proven primary, including rejected unknown targets.
        task_id = (
            None
            if isinstance(
                record, (AcceptDecision, BoundScopeResponseDecision, BoundReassignDecision)
            )
            else getattr(record, "task_id", None)
        )
        task = await self._task_or_none(record.run_id, task_id, True)
        attempt_id = getattr(record, "attempt_id", None)
        if attempt_id is not None:
            attempt = await self._session.get(SubscriptionAttempt, attempt_id, with_for_update=True)
            if attempt is None or task is None or attempt.task_row_id != task.id:
                raise SubscriptionConflict("foreign attempt lineage")
        payload = encode_subscription_record(record)
        row = (
            await self._session.execute(
                select(SubscriptionDecisionRecord)
                .where(
                    SubscriptionDecisionRecord.run_id == record.run_id,
                    SubscriptionDecisionRecord.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            self._session.add(
                SubscriptionDecisionRecord(
                    run_id=record.run_id,
                    task_row_id=None if task is None else task.id,
                    attempt_id=attempt_id,
                    record_type=type(record).__name__,
                    idempotency_key=idempotency_key,
                    payload=payload,
                )
            )
            await self._session.flush()
            return record
        if row.payload != payload or row.record_type != type(record).__name__:
            raise SubscriptionConflict("decision replay conflicts")
        return self._decode(row.payload, type(record))

    async def _task_or_none(
        self, run_id: UUID, task_id: UUID | None, lock: bool
    ) -> SubscriptionTask | None:
        return None if task_id is None else await self._task(run_id, task_id, lock)

    async def _task(self, run_id: UUID, task_id: UUID, lock: bool) -> SubscriptionTask:
        row = await self._session.get(SubscriptionTask, task_id, with_for_update=lock)
        if row is None or row.run_id != run_id:
            raise SubscriptionConflict("foreign task lineage")
        return row

    async def get_task(self, run_id: UUID, task_id: UUID) -> LogicalTaskContract:
        row = await self._task(run_id, task_id, False)
        return self._decode(row.payload, LogicalTaskContract)

    async def invocation_tasks(
        self, run_id: UUID, excluding_task_id: UUID
    ) -> tuple[LogicalTaskContract, ...]:
        rows = (
            await self._session.scalars(
                select(SubscriptionTask)
                .where(SubscriptionTask.run_id == run_id, SubscriptionTask.id != excluding_task_id)
                .order_by(SubscriptionTask.id)
                .limit(257)
            )
        ).all()
        if len(rows) > 256:
            raise SubscriptionConflict("invocation task context exceeds its bound")
        contracts = tuple(self._decode(row.payload, LogicalTaskContract) for row in rows)
        if any(
            contract.run_id != run_id or contract.task_id != row.id
            for row, contract in zip(rows, contracts, strict=True)
        ):
            raise SubscriptionConflict("invocation task context identity differs")
        return contracts

    async def invocation_outcomes(
        self, run_id: UUID, task_ids: tuple[UUID, ...]
    ) -> tuple[InvocationTaskOutcome, ...]:
        if not 1 <= len(task_ids) <= 257 or len(set(task_ids)) != len(task_ids):
            raise SubscriptionConflict("invocation outcome task set exceeds its bound")
        latest = (
            select(SubscriptionDecisionRecord.id)
            .join(
                SubscriptionAttempt, SubscriptionAttempt.id == SubscriptionDecisionRecord.attempt_id
            )
            .where(
                SubscriptionDecisionRecord.run_id == run_id,
                SubscriptionDecisionRecord.task_row_id == SubscriptionTask.id,
                SubscriptionDecisionRecord.record_type.in_(("TaskHandoff", "ReviewedTaskHandoff")),
                SubscriptionAttempt.run_id == run_id,
                SubscriptionAttempt.task_row_id == SubscriptionTask.id,
            )
            .order_by(
                SubscriptionAttempt.attempt_number.desc(),
                SubscriptionDecisionRecord.created_at.desc(),
                SubscriptionDecisionRecord.id.desc(),
            )
            .limit(1)
            .correlate(SubscriptionTask)
            .scalar_subquery()
        )
        # Cap each transferred handoff before decoding. The request constructor
        # independently bounds the aggregate untrusted context; never truncate
        # evidence or silently omit an oversized/invalid latest handoff.
        payload = case(
            (
                func.octet_length(cast(SubscriptionDecisionRecord.payload, Text)) <= 65536,
                SubscriptionDecisionRecord.payload,
            ),
            else_=None,
        )
        rows = (
            await self._session.execute(
                select(
                    SubscriptionTask.id,
                    SubscriptionTask.state,
                    SubscriptionTask.version,
                    SubscriptionTask.pause_requested,
                    SubscriptionTask.cancel_requested,
                    SubscriptionDecisionRecord.attempt_id,
                    payload,
                    SubscriptionDecisionRecord.id,
                )
                .outerjoin(SubscriptionDecisionRecord, SubscriptionDecisionRecord.id == latest)
                .where(SubscriptionTask.run_id == run_id, SubscriptionTask.id.in_(task_ids))
                .order_by(SubscriptionTask.id)
            )
        ).all()
        if len(rows) != len(task_ids):
            raise SubscriptionConflict("invocation outcome task lineage differs")
        outcomes = []
        for task_id, state, version, paused, cancelled, attempt_id, value, record_id in rows:
            handoff = None
            if record_id is not None:
                if value is None:
                    raise SubscriptionConflict("invocation handoff exceeds its bound")
                handoff = self._decode(value, TaskHandoff)
                if (handoff.run_id, handoff.task_id, handoff.attempt_id) != (
                    run_id,
                    task_id,
                    attempt_id,
                ):
                    raise SubscriptionConflict("invocation handoff lineage differs")
            pending = (
                await PostgresSchedulingRepository(self._session).pending_scope_request(
                    run_id, task_id
                )
                if state == "blocked"
                else None
            )
            answer = await self._scope_response_for_context(run_id, task_id)
            from forge.persistence.repositories.subscription_task_acceptance import (
                task_acceptance_for_context,
            )

            acceptance = await task_acceptance_for_context(self._session, run_id, task_id)
            outcomes.append(
                InvocationTaskOutcome(
                    task_id,
                    state,
                    version,
                    paused,
                    cancelled,
                    handoff,
                    pending[0] if pending else None,
                    pending[1] if pending else None,
                    answer[0] if answer else None,
                    answer[1] if answer else None,
                    acceptance[0] if acceptance else None,
                    acceptance[1] if acceptance else None,
                    acceptance[2] if acceptance else None,
                )
            )
        return tuple(outcomes)

    async def _scope_response_for_context(
        self, run_id: UUID, task_id: UUID
    ) -> tuple[UUID, BoundScopeResponseDecision] | None:
        # Version-one records encode dataclass fields as ordered name/value pairs.
        target = SubscriptionDecisionRecord.payload["record"]["fields"][1][1]["$uuid"].astext
        row = (
            await self._session.execute(
                select(SubscriptionDecisionRecord, SubscriptionAttemptResult)
                .join(
                    SubscriptionAttempt,
                    SubscriptionAttempt.id == SubscriptionDecisionRecord.attempt_id,
                )
                .join(
                    SubscriptionAttemptResult,
                    SubscriptionAttemptResult.attempt_id == SubscriptionAttempt.id,
                )
                .where(
                    SubscriptionDecisionRecord.run_id == run_id,
                    SubscriptionAttempt.run_id == run_id,
                    SubscriptionDecisionRecord.record_type == "BoundScopeResponseDecision",
                    target == str(task_id),
                    SubscriptionAttemptResult.accepted.is_(True),
                    SubscriptionAttemptResult.disposition == "scope_responded",
                )
                .order_by(SubscriptionAttempt.attempt_number.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        record, result = row
        response = self._decode(record.payload, BoundScopeResponseDecision)
        receipt = result.application_payload
        if (
            receipt is None
            or canonical_digest(receipt) != result.application_digest
            or canonical_digest(result.result_payload) != result.result_digest
            or record.payload != result.result_payload.get("decision")
            or receipt.get("kind") != "scope_response"
            or receipt.get("response_result_digest") != result.result_digest
            or receipt.get("child_task_id") != str(task_id)
            or receipt.get("request_attempt_id") != str(response.request_attempt_id)
            or response.run_id != run_id
            or response.task_id != task_id
            or record.idempotency_key != f"scope-response:{result.attempt_id}"
        ):
            raise SubscriptionConflict("invocation scope response differs")
        return result.attempt_id, response

    async def _pool(
        self, run_id: UUID, task_row_id: UUID | None, lock: bool
    ) -> SubscriptionBudgetPool | None:
        predicate = (
            SubscriptionBudgetPool.task_row_id.is_(None)
            if task_row_id is None
            else SubscriptionBudgetPool.task_row_id == task_row_id
        )
        stmt = select(SubscriptionBudgetPool).where(
            SubscriptionBudgetPool.run_id == run_id, predicate
        )
        if lock:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()
