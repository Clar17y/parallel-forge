"""Settle an interrupted validation admission without certifying partial checks."""

from __future__ import annotations

import hashlib
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.executions import ExecutionStatus
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunSnapshot, RunState
from forge.domain.validation import command_spec_digest


async def cancel_retained_validation(
    work: UnitOfWork, run: RunSnapshot, source: CommandEnvelope
) -> UUID:
    """Cancel an exactly bound controller step in the resume transaction.

    The caller verifies the current resume and causal pause, cancels the expired
    source lease, and must prove whole-run quiescence before committing. Any
    unresolved operation or tool call therefore rolls this settlement back.
    Completed check receipts remain immutable; fresh validation reruns checks.
    """
    if (
        run.state is not RunState.PAUSED
        or run.suspended_state is not RunState.VALIDATING
        or source.run_id != run.id
        or source.command_type != "validate"
        or source.status is not CommandStatus.CANCELLED
        or source.payload_schema_version != 1
        or source.expected_run_version != run.version - 1
        or run.policy_version is None
    ):
        raise CommandRecoveryRequired("retained validation source is invalid")
    attempt = source.payload.get("semantic_attempt")
    step_id = uuid5(NAMESPACE_URL, f"forge:validate:{source.id}")
    step = await work.controller_steps.get(run.id, step_id)
    if (
        type(attempt) is not int
        or step is None
        or step.kind != "validate"
        or step.attempt != attempt
        or step.status is not ExecutionStatus.RUNNING
    ):
        raise CommandRecoveryRequired("retained validation admission is invalid")
    policy_record = await work.projects.get_policy(
        run.project_id, run.policy_version, for_update=True
    )
    try:
        policy = ProjectPolicy.model_validate(policy_record.document)
    except ValueError:
        raise CommandRecoveryRequired("retained validation policy is invalid") from None
    policy_wire = json.dumps(
        policy_record.document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if (
        policy.id != run.project_id
        or policy.version != run.policy_version
        or policy_record.version != run.policy_version
        or policy_record.document_schema_version != 1
        or hashlib.sha256(policy_wire).hexdigest() != policy_record.policy_digest
    ):
        raise CommandRecoveryRequired("retained validation policy binding differs")
    events = [
        event
        for event in await work.events.list_after(run.id, 0)
        if event.event_type == "run.validation_started"
        and event.payload.get("step_id") == str(step_id)
    ]
    if len(events) != 1:
        raise CommandRecoveryRequired("retained validation has no unique admission event")
    event = events[0]
    head = event.payload.get("head_sha")
    expected = {
        "command_id": str(source.id),
        "step_id": str(step_id),
        "head_sha": head,
        "policy_version": run.policy_version,
        "evidence_set_id": str(uuid5(step_id, "validation-evidence")),
        "checks": tuple(command_spec_digest(spec) for spec in policy.required_checks),
    }
    if source.payload.get("prior_review_evidence_set_id") is not None:
        expected["prior_review_evidence_set_id"] = source.payload["prior_review_evidence_set_id"]
    if (
        event.run_version != source.expected_run_version
        or event.actor_class != "worker"
        or event.actor_id is not None
        or event.payload_schema_version != 1
        or event.payload != expected
        or not isinstance(head, str)
        or len(head) != 40
        or any(character not in "0123456789abcdef" for character in head)
    ):
        raise CommandRecoveryRequired("retained validation admission binding differs")
    await work.controller_steps.finalize(
        run.id,
        step_id,
        ExecutionStatus.CANCELLED,
        outcome="operator pause reconciled validation without publishing partial checks",
    )
    return step_id


__all__ = ["cancel_retained_validation"]
