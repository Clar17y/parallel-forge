from uuid import uuid4

import pytest
from forge.application.services.projects import _policy_document
from forge.domain.policy import JevPolicy, ProjectPolicy
from pydantic import ValidationError


def test_legacy_policy_has_no_remote_consent(tmp_path):
    policy = ProjectPolicy.model_validate(
        {
            "id": uuid4(),
            "version": 1,
            "repository_path": str(tmp_path),
            "github_repository": "o/r",
            "default_branch": "main",
        }
    )
    assert policy.jev is None
    assert "jev" not in _policy_document(policy)


def test_jev_accepts_unpinned_bounded_alias_but_rejects_whitespace_and_coercion():
    assert JevPolicy(model="future-provider-alias").model == "future-provider-alias"
    for value in ("", " ", " jev-latest", "jev-latest ", "x" * 129):
        with pytest.raises(ValidationError):
            JevPolicy(model=value)
    with pytest.raises(ValidationError):
        JevPolicy(max_requests_per_run="64")
