from __future__ import annotations

import pytest
from forge.application.ports.worktrees import GitCandidateFile
from forge.application.services.dependency_scope import (
    DependencyScopeError,
    dependency_deltas,
    verify_dependency_declarations,
)


def _file(path: str, before: bytes, after: bytes) -> GitCandidateFile:
    return GitCandidateFile(path=path, head_sha="b" * 40, base_content=before, head_content=after)


def test_nested_node_delta_requires_exact_group_and_package() -> None:
    snapshot = _file(
        "apps/web/package.json",
        b'{"dependencies":{"react":"18"}}',
        b'{"dependencies":{"react":"19","vite":"5"}}',
    )
    deltas = dependency_deltas(snapshot)
    assert {delta.declaration() for delta in deltas} == {
        'dependency:v1:{"after":"19","before":"18","group":"dependencies","package":"react","path":"apps/web/package.json"}',
        'dependency:v1:{"after":"5","before":null,"group":"dependencies","package":"vite","path":"apps/web/package.json"}',
    }
    assert (
        verify_dependency_declarations(deltas, tuple(delta.declaration() for delta in deltas))
        is None
    )


def test_extra_package_or_duplicate_declaration_is_rejected() -> None:
    snapshot = _file("package.json", b'{"dependencies":{"a":"1"}}', b'{"dependencies":{"a":"2"}}')
    delta = dependency_deltas(snapshot)[0].declaration()
    assert (
        verify_dependency_declarations(dependency_deltas(snapshot), (delta, delta))
        == "dependency_authorization_is_ambiguous"
    )
    extra = delta.replace('"package":"a"', '"package":"b"')
    assert (
        verify_dependency_declarations(dependency_deltas(snapshot), (extra,))
        == "undeclared_dependency_change"
    )


def test_unsupported_manifest_and_ambiguous_python_data_fail_closed() -> None:
    with pytest.raises(DependencyScopeError):
        dependency_deltas(_file("requirements.txt", b"a==1\n", b"a==2\n"))
    with pytest.raises(DependencyScopeError):
        dependency_deltas(
            _file(
                "pyproject.toml",
                b"[project]\ndependencies = ['a==1']\n",
                b"[project]\ndependencies = 'a==2'\n",
            )
        )


def test_metadata_only_supported_manifest_change_has_no_dependency_delta() -> None:
    snapshot = _file(
        "package.json",
        b'{"name":"old","dependencies":{"a":"1"}}',
        b'{"name":"new","dependencies":{"a":"1"}}',
    )
    assert dependency_deltas(snapshot) == ()


def test_changed_unsupported_python_dependency_table_fails_closed() -> None:
    with pytest.raises(DependencyScopeError):
        dependency_deltas(
            _file(
                "pyproject.toml",
                b"[tool.poetry]\ndependencies = {demo = '1'}\n",
                b"[tool.poetry]\ndependencies = {demo = '2'}\n",
            )
        )


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "pyproject.toml",
            b'[build-system]\nbuild-backend="a"\n',
            b'[build-system]\nbuild-backend="b"\n',
        ),
        ("package.json", b'{"bundledDependencies":[]}', b'{"bundledDependencies":["hidden"]}'),
    ],
)
def test_changed_unsupported_dependency_controls_intervene(path, before, after):
    with pytest.raises(DependencyScopeError):
        dependency_deltas(_file(path, before, after))


def test_python_requirement_alias_duplicates_are_ambiguous() -> None:
    with pytest.raises(DependencyScopeError):
        dependency_deltas(
            _file(
                "pyproject.toml",
                b"[project]\ndependencies = ['Foo_Bar==1', 'foo-bar==1']\n",
                b"[project]\ndependencies = ['Foo_Bar==1', 'foo-bar==2']\n",
            )
        )
