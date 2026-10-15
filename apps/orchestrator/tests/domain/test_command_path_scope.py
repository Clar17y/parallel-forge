"""Focused named checks: Forge appends owned test paths; agents never supply argv."""

from __future__ import annotations

import hashlib
import json

import pytest
from forge.domain.policy import (
    CommandPathScope,
    CommandSpec,
    StepKind,
    scoped_check_paths,
    scoped_command,
)
from forge.domain.validation import command_spec_digest
from pydantic import ValidationError


def focused(**scope: object) -> CommandSpec:
    return CommandSpec(
        kind=StepKind.TEST,
        name="test-focused",
        argv=(".venv/bin/python", "-m", "pytest", "-q"),
        timeout_seconds=900,
        path_scope=CommandPathScope(prefixes=("apps/orchestrator/tests", "tests/"), **scope),
    )


def test_prefixes_are_normalized_directories_and_unsafe_prefixes_are_rejected() -> None:
    assert focused().path_scope.prefixes == ("apps/orchestrator/tests/", "tests/")  # type: ignore[union-attr]
    for prefix in ("../tests", "/abs/tests", "-k", "apps\tests", "", "apps/../tests", "a;b"):
        with pytest.raises(ValidationError):
            CommandPathScope(prefixes=(prefix,))


def test_owned_paths_under_a_prefix_are_selected_sorted_and_deduplicated() -> None:
    owned = (
        "apps/orchestrator/src/forge/domain/policy.py",
        "apps/orchestrator/tests/domain/test_policy.py",
        "tests/integration",
        "apps/orchestrator/tests/domain/test_policy.py",
        "apps/orchestrator/testsuite/other.py",
    )
    assert scoped_check_paths(focused(), owned) == (
        "apps/orchestrator/tests/domain/test_policy.py",
        "tests/integration",
    )


def test_no_owned_test_path_selects_nothing_and_never_falls_back_to_the_full_suite() -> None:
    assert scoped_check_paths(focused(), ("apps/orchestrator/src/forge/domain/policy.py",)) == ()
    with pytest.raises(ValueError):
        scoped_command(focused(), ())


def test_too_many_owned_test_paths_is_refused_rather_than_truncated() -> None:
    owned = tuple(f"tests/test_{index}.py" for index in range(3))
    with pytest.raises(ValueError):
        scoped_check_paths(focused(max_paths=2), owned)


def test_scoped_command_appends_paths_and_binds_its_own_digest() -> None:
    base = focused()
    derived = scoped_command(base, ("apps/orchestrator/tests/domain/test_policy.py",))
    assert derived.argv == (*base.argv, "apps/orchestrator/tests/domain/test_policy.py")
    assert derived.path_scope is None and derived.name == base.name
    assert command_spec_digest(derived) != command_spec_digest(base)
    for path in ("apps/orchestrator/src/forge/x.py", "-p", "tests/../secrets", "tests/a b;c"):
        with pytest.raises((ValueError, ValidationError)):
            scoped_command(base, (path,))
    with pytest.raises(ValueError):
        scoped_command(CommandSpec(kind=StepKind.TEST, name="t", argv=("pytest",), timeout_seconds=9), ("tests/x.py",))


def test_unscoped_command_digest_is_unchanged_by_the_new_field() -> None:
    spec = CommandSpec(kind=StepKind.TEST, name="unit", argv=("pytest",), timeout_seconds=30)
    legacy = {key: value for key, value in spec.model_dump(mode="json").items() if key != "path_scope"}
    expected = hashlib.sha256(
        json.dumps(legacy, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert command_spec_digest(spec) == expected
