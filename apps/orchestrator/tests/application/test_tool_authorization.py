from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest
from forge.application.ports.tools import ToolAuthorizationDenied
from forge.application.services.tools import CapabilityMatrix, ToolAuthorizer
from forge.domain.actor import AgentRole
from forge.domain.tool import ToolAuthorizationContext, ToolName, ToolRequest

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")

EXPECTED_CAPABILITIES = {
    AgentRole.PLANNER: frozenset(
        {
            ToolName.REPOSITORY_LIST_FILES,
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_SEARCH,
            ToolName.REPOSITORY_READ_INSTRUCTIONS,
        }
    ),
    AgentRole.DEVELOPER: frozenset(
        {
            ToolName.REPOSITORY_LIST_FILES,
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_SEARCH,
            ToolName.REPOSITORY_READ_INSTRUCTIONS,
            ToolName.REPOSITORY_WRITE_FILE,
            ToolName.GIT_STATUS,
            ToolName.GIT_DIFF,
            ToolName.GIT_COMMIT,
            ToolName.BUILD_RUN_NAMED_CHECK,
        }
    ),
    AgentRole.REVIEWER: frozenset(
        {
            ToolName.REPOSITORY_LIST_FILES,
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_SEARCH,
            ToolName.REPOSITORY_READ_INSTRUCTIONS,
            ToolName.GIT_STATUS,
            ToolName.GIT_DIFF,
            ToolName.VALIDATION_RESULTS_READ,
            ToolName.REVIEW_ARTIFACTS_READ,
        }
    ),
}


def _context(role: AgentRole = AgentRole.PLANNER) -> ToolAuthorizationContext:
    return ToolAuthorizationContext(
        role=role,
        run_id=RUN_ID,
        worktree_id="forge-111111111111-222222222222",
        policy_version=7,
    )


def test_capability_matrix_exactly_matches_every_role_and_tool_decision() -> None:
    matrix = CapabilityMatrix()

    assert matrix.roles == frozenset(AgentRole)
    assert matrix.tools == frozenset(ToolName)
    for role in AgentRole:
        assert matrix.capabilities_for(role) == EXPECTED_CAPABILITIES[role]
        for tool_name in ToolName:
            assert matrix.is_allowed(role, tool_name) is (tool_name in EXPECTED_CAPABILITIES[role])


@pytest.mark.parametrize("role", tuple(AgentRole))
@pytest.mark.parametrize("tool_name", tuple(ToolName))
def test_authorizer_matches_the_exhaustive_matrix(
    role: AgentRole,
    tool_name: ToolName,
) -> None:
    authorizer = ToolAuthorizer()

    assert authorizer.is_allowed(role, tool_name) is (tool_name in EXPECTED_CAPABILITIES[role])


@pytest.mark.parametrize(
    ("role", "tool_name"),
    [
        ("planner", ToolName.REPOSITORY_READ_FILE),
        (AgentRole.PLANNER, "repository.read_file"),
        ("administrator", "release.merge_pull_request"),
        (None, None),
    ],
)
def test_unknown_or_untyped_role_and_tool_values_deny_by_default(
    role: object,
    tool_name: object,
) -> None:
    authorizer = ToolAuthorizer()

    assert authorizer.is_allowed(role, tool_name) is False  # type: ignore[arg-type]


def test_authorize_returns_only_context_bound_authority() -> None:
    request = ToolRequest(
        name=ToolName.REPOSITORY_READ_FILE,
        arguments={"path": "src/forge/domain/run.py"},
    )

    authorization = ToolAuthorizer().authorize(_context(), request)

    assert authorization.role is AgentRole.PLANNER
    assert authorization.run_id == RUN_ID
    assert authorization.worktree_id == "forge-111111111111-222222222222"
    assert authorization.policy_version == 7
    assert authorization.tool_name is ToolName.REPOSITORY_READ_FILE
    assert dict(authorization.arguments) == {"path": "src/forge/domain/run.py"}

    with pytest.raises((FrozenInstanceError, TypeError)):
        authorization.policy_version = 8  # type: ignore[misc]
    with pytest.raises(TypeError):
        authorization.arguments["path"] = "elsewhere"  # type: ignore[index]


def test_denial_is_stable_and_redacts_requested_arguments() -> None:
    secret = "github_pat_very_secret_value"
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "release.txt", "content": secret},
    )

    with pytest.raises(ToolAuthorizationDenied) as captured:
        ToolAuthorizer().authorize(_context(), request)

    assert str(captured.value) == "tool authorization denied"
    assert repr(captured.value) == "ToolAuthorizationDenied('tool authorization denied')"
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)


def test_invalid_authorization_boundary_values_fail_with_the_same_redacted_denial() -> None:
    authorizer = ToolAuthorizer()
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={})

    for context, candidate_request in (
        ("planner", request),
        (_context(), "repository.read_file"),
        (object(), object()),
    ):
        with pytest.raises(ToolAuthorizationDenied, match=r"^tool authorization denied$"):
            authorizer.authorize(context, candidate_request)  # type: ignore[arg-type]
