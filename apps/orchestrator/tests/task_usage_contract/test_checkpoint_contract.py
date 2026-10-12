from uuid import uuid4

import pytest
from forge.api.schemas.task_usage import TaskUsageOwnerCommandRequest
from forge.application.services.task_usage_composition import configured_run_cohort
from forge.domain.subscription import ExecutionEnvelope, RouteBinding, RouteSpec, SpecialistPurpose
from forge.domain.task_usage_contract import OwnerCommand
from pydantic import ValidationError


def test_owner_can_stop_a_checkpoint_without_a_note() -> None:
    command = OwnerCommand(
        command_id=uuid4(),
        checkpoint_id=uuid4(),
        expected_version=2,
        idempotency_key="stop-once",
        action="stop",
        actor_id=uuid4(),
    )
    assert command.action == "stop"


def test_command_wire_rejects_spoofed_actor_and_out_of_range_version() -> None:
    valid = {
        "checkpoint_id": str(uuid4()),
        "expected_version": 0,
        "idempotency_key": "owner-next",
        "action": "continue",
        "continue_mode": "next_checkpoint",
    }
    assert TaskUsageOwnerCommandRequest.model_validate(valid).expected_version == 0
    with pytest.raises(ValidationError):
        TaskUsageOwnerCommandRequest.model_validate({**valid, "actor_id": str(uuid4())})
    with pytest.raises(ValidationError):
        TaskUsageOwnerCommandRequest.model_validate({**valid, "expected_version": 2**63})
    with pytest.raises(ValidationError):
        TaskUsageOwnerCommandRequest.model_validate({**valid, "expected_version": True})


def test_run_cohort_uses_frozen_primary_and_child_routes_in_stable_order() -> None:
    primary = RouteBinding(requested=RouteSpec(provider="openai", client="codex", model="primary"), effective=RouteSpec(provider="openai", client="codex", model="primary"), is_primary=True)
    child = RouteBinding(requested=RouteSpec(provider="google", client="gemini", model="child"), effective=RouteSpec(provider="google", client="gemini", model="child"))
    routes = ((SpecialistPurpose.ROUTINE_IMPLEMENTATION, child), (SpecialistPurpose.PRIMARY, primary))
    envelope = ExecutionEnvelope(run_id=uuid4(), profile_id=uuid4(), profile_version=1, safety_policy_version=1, routes=routes)
    cohort = configured_run_cohort(envelope)
    reverse = ExecutionEnvelope(run_id=envelope.run_id, profile_id=envelope.profile_id, profile_version=1, safety_policy_version=1, routes=tuple(reversed(routes)))
    assert cohort == configured_run_cohort(reverse)
    assert {member.role for member in cohort.members} == {"primary", "routine_implementation"}
    assert {member.model for member in cohort.members} == {"primary", "child"}
