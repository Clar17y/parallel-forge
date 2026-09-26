"""Subscription scope is explicit while legacy plan bytes remain unchanged."""

import json

import pytest
from forge.domain.plan import PlanOutput


def legacy_plan():
    return PlanOutput(
        summary="Update component",
        assumptions=(),
        affected_components=("API service",),
        steps=("Implement",),
        required_checks=("unit",),
        risks=("Regression",),
        security_considerations=(),
        dependency_changes=(),
    )


def test_scoped_plan_retains_legacy_representation():
    from forge.domain.plan import ScopedPlanOutput, decode_plan_output

    legacy = legacy_plan()
    original = legacy.model_dump_json()
    assert type(decode_plan_output(original)) is PlanOutput
    assert decode_plan_output(original).model_dump_json() == original
    scoped = ScopedPlanOutput(**legacy.model_dump(), owned_paths=("apps/api", "tests/api"))
    assert decode_plan_output(scoped.model_dump_json()) == scoped
    assert json.loads(scoped.model_dump_json())["owned_paths"] == ["apps/api", "tests/api"]
    assert "owned_paths" not in PlanOutput.model_json_schema()["properties"]
    assert "owned_paths" in ScopedPlanOutput.model_json_schema()["required"]


@pytest.mark.parametrize(
    "paths",
    [
        ("../outside",),
        (".git/config",),
        ("C:/dev",),
        ("src\\file",),
        ("src", "src"),
        tuple(f"p{i}" for i in range(65)),
    ],
)
def test_scoped_plan_rejects_invalid_paths(paths):
    from forge.domain.plan import ScopedPlanOutput

    with pytest.raises(ValueError):
        ScopedPlanOutput(**legacy_plan().model_dump(), owned_paths=paths)


def test_explicit_empty_scope_grants_no_writable_paths():
    from forge.domain.plan import ScopedPlanOutput

    assert ScopedPlanOutput(**legacy_plan().model_dump(), owned_paths=()).owned_paths == ()


@pytest.mark.parametrize("name", ["unit tests", "unit.test", "unit\n", "unit;exit"])
def test_scoped_checks_are_valid_subscription_command_identifiers(name):
    from forge.domain.plan import ScopedPlanOutput

    values = dict(legacy_plan().model_dump(), required_checks=(name,))
    assert PlanOutput(**values).required_checks == (name,)
    with pytest.raises(ValueError):
        ScopedPlanOutput(**values, owned_paths=("src",))
