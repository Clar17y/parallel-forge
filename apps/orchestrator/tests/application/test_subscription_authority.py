from uuid import uuid4

import pytest
from forge.domain.subscription import SpecialistPurpose
from forge.domain.tool import SubscriptionToolAuthorizationContext, ToolName


def test_subscription_authority_binds_purpose_and_capability_intersection():
    authority = SubscriptionToolAuthorizationContext(
        run_id=uuid4(), task_id=uuid4(), attempt_id=uuid4(), worktree_id="forge-test",
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION, policy_version=1,
        permitted_tools=frozenset({ToolName.REPOSITORY_WRITE_FILE}),
    )
    assert authority.purpose is SpecialistPurpose.ROUTINE_IMPLEMENTATION


def test_subscription_authority_rejects_tool_outside_actual_purpose():
    with pytest.raises(ValueError):
        SubscriptionToolAuthorizationContext(
            run_id=uuid4(), task_id=uuid4(), attempt_id=uuid4(), worktree_id="forge-test",
            purpose=SpecialistPurpose.PLANNING, policy_version=1,
            permitted_tools=frozenset({ToolName.REPOSITORY_WRITE_FILE}),
        )
