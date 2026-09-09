"""Remote cockpit evidence preserves observation identity and excludes private metadata."""

from copy import deepcopy

import pytest
from forge.persistence.models import PullRequest
from forge.persistence.queries.dashboard import _remote_observation
from forge.persistence.repositories.runs import PersistenceDataError


def observation():
    binding = {"observation_digest": "a" * 64, "head_sha": "b" * 40}
    return PullRequest(
        checks_schema_version=1,
        reviews_schema_version=1,
        checks=binding
        | {
            "items": [
                {
                    "name": "ci",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": "b" * 40,
                    "summary": "checked",
                    "text": None,
                    "details_url": "javascript:untrusted()",
                    "private_reasoning": "never expose",
                }
            ]
        },
        review_state=binding | {"items": []},
    )


@pytest.mark.parametrize("drift", ["head", "digest", "version", "shape"])
def test_remote_observation_refuses_mixed_or_unknown_evidence(drift):
    row = observation()
    row.review_state = deepcopy(row.review_state)
    if drift == "head":
        row.review_state["head_sha"] = "c" * 40
    elif drift == "digest":
        row.review_state["observation_digest"] = "d" * 64
    elif drift == "version":
        row.reviews_schema_version = 2
    else:
        row.checks["items"] = ["unstructured"]
    with pytest.raises(PersistenceDataError):
        _remote_observation(row)


def test_remote_observation_selects_evidence_without_remote_navigation_or_private_fields():
    result = _remote_observation(observation())
    assert result["observation_digest"] == "a" * 64
    assert result["checks"][0]["summary"] == "checked"
    assert "private_reasoning" not in result["checks"][0]
    assert "details_url" not in result["checks"][0]
    assert _remote_observation(None) is None
    assert _remote_observation(PullRequest(checks={}, review_state={})) is None
