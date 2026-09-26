"""Primary commit authority has an explicit scoped codec; historical codecs stay fixed."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.application.adapters.git_commit import (
    PREPARE_GIT_COMMIT_KIND,
    GitCommitOperationError,
    PrepareGitCommitAdapter,
    PublishGitCommitAdapter,
)
from forge.domain.operation import OperationStatus

from apps.orchestrator.tests.application.test_git_commit_adapters import (
    _Git,
    _intent,
    _payload,
    _publication_intent,
    _Reader,
    _worktree,
)


def primary_payload(tree, run_id):
    values = _payload(tree, run_id)
    del values["agent_execution_id"], values["step_id"]
    return values | {
        "authority_schema_version": 3,
        "subscription_task_id": str(uuid4()),
        "subscription_attempt_id": str(uuid4()),
        "subscription_purpose": "primary",
        "owned_paths_json": '["docs/result.md"]',
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("authority_schema_version", 2),
        ("authority_schema_version", True),
        ("authority_schema_version", {}),
        ("subscription_purpose", "integration"),
        ("owned_paths_json", "[]"),
        ("owned_paths_json", '["../docs"]'),
        ("owned_paths_json", '["docs", "docs"]'),
        ("owned_paths_json", '[ "docs" ]'),
        ("owned_paths_json", '{"docs":true}'),
    ],
)
async def test_invalid_primary_authority_never_prepares(field, value):
    run_id = uuid4()
    tree = _worktree(run_id)
    git = _Git(tree)
    payload = primary_payload(tree, run_id) | {field: value}
    if value == 2:
        payload.pop("owned_paths_json", None)
    with pytest.raises(GitCommitOperationError):
        await PrepareGitCommitAdapter(git, tree).invoke(
            _intent(PREPARE_GIT_COMMIT_KIND, run_id, payload)
        )
    assert git.prepare_calls == 0


@pytest.mark.parametrize("tampered", [None, "request", "receipt"])
async def test_scope_is_bound_across_prepare_publish_and_readonly_recovery(tampered):
    run_id = uuid4()
    tree = _worktree(run_id)
    git = _Git(tree)
    payload = primary_payload(tree, run_id)
    intent = _intent(PREPARE_GIT_COMMIT_KIND, run_id, payload)
    outcome = await PrepareGitCommitAdapter(git, tree).invoke(intent)
    assert git.allowed_paths == ("docs/result.md",) and git.prepare_calls == 1
    prepared = replace(
        intent,
        status=OperationStatus.SUCCEEDED,
        outcome=outcome.payload,
        outcome_schema_version=1,
        completed_at=datetime.now(UTC),
    )
    publication_payload = payload
    if tampered == "request":
        publication_payload = payload | {"owned_paths_json": '["src"]'}
    elif tampered == "receipt":
        prepared = replace(
            prepared, outcome=dict(outcome.payload) | {"owned_paths_json": '["src"]'}
        )
    publication = _publication_intent(tree, run_id, prepared, publication_payload)
    adapter = PublishGitCommitAdapter(git, tree, _Reader(prepared))
    if tampered:
        with pytest.raises(GitCommitOperationError):
            await adapter.invoke(publication)
        assert git.publish_calls == 0
        return
    published = await adapter.invoke(publication)
    assert published.payload["owned_paths_json"] == '["docs/result.md"]'
    assert await adapter.reconcile(publication) == published
    assert git.prepare_calls == git.publish_calls == git.inspect_calls == 1
