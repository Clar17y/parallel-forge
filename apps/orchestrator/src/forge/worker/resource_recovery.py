"""Bind resource inspection to persisted run and project policy."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.operations import OperationAdapter
from forge.application.services.recovery import RecoveryError
from forge.domain.operation import OperationIntent, OperationOutcome, canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.persistence.unit_of_work import PostgresUnitOfWork

if TYPE_CHECKING:
    from forge.worker.delivery_runtime import DeliveryRuntime


def resource_recovery_adapters(
    factory: async_sessionmaker[AsyncSession], runtime: DeliveryRuntime
) -> dict[str, OperationAdapter]:
    return {
        kind: _ResourceRecovery(factory, runtime, kind)
        for kind in (
            "worktree.create", "worktree.teardown", "database.provision", "database.teardown",
            "worktree.environment.stage", "worktree.setup.command",
        )
    }


class _ResourceRecovery:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], runtime: DeliveryRuntime, kind: str
    ) -> None:
        self._factory, self._runtime, self._kind = factory, runtime, kind

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        raise RecoveryError("startup resource adapters cannot invoke effects")

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        if (
            intent.kind != self._kind
            or intent.request_schema_version != 1
            or intent.request_digest != canonical_digest(intent.request_payload)
        ):
            raise RecoveryError("startup resource request differs")
        async with PostgresUnitOfWork(self._factory) as work:
            run = await work.runs.get_for_update(intent.run_id)
            if run.policy_version is None:
                raise RecoveryError("startup resource policy is absent")
            project = await work.projects.get(run.project_id, for_update=True)
            record = await work.projects.get_policy(run.project_id, run.policy_version, for_update=True)
            policy = ProjectPolicy.model_validate(record.document)
            wire = json.dumps(
                record.document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            if (
                record.document_schema_version != 1
                or record.policy_digest != hashlib.sha256(wire).hexdigest()
                or record.version != run.policy_version
                or policy.version != run.policy_version
                or policy.id != run.project_id
                or policy.repository_path != project.canonical_path
                or policy.github_repository != project.github_repository
                or policy.default_branch != project.default_branch
                or type(intent.request_payload.get("policy_version")) is not int
                or intent.request_payload["policy_version"] != run.policy_version
            ):
                raise RecoveryError("startup resource policy differs")
        # Existing resource adapters validate their full request against this run/policy,
        # and worktree adapters additionally prove durable resource checkpoints.
        adapter = self._runtime.resource_recovery_adapter(run, policy, kind=self._kind)
        return await adapter.reconcile(intent)
