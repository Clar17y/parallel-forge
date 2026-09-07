"""Fail-closed comparison of approved dependency declarations.

Only ``pyproject.toml`` (PEP 621/build-system tables) and ``package.json``
ordinary dependency sections are supported.  Declarations retain the existing
string field while using canonical values such as::

    dependency:v1:{"after":"^2.1.0","before":"^2.0.0","group":"dependencies","package":"react","path":"apps/web/package.json"}
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from forge.application.ports.worktrees import GitCandidateFile
from forge.domain.paths import normalize_policy_path


class DependencyScopeError(ValueError):
    """Dependency manifest content or declaration is unsupported or ambiguous."""


@dataclass(frozen=True, slots=True)
class DependencyDelta:
    path: str
    group: str
    package: str
    before: str | None
    after: str | None

    def declaration(self) -> str:
        return "dependency:v1:" + json.dumps(
            {
                "after": self.after,
                "before": self.before,
                "group": self.group,
                "package": self.package,
                "path": self.path,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


_NODE_GROUPS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
_UNSUPPORTED_NODE_KEYS = frozenset(
    {
        "bundledDependencies",
        "bundleDependencies",
        "overrides",
        "resolutions",
        "pnpm",
        "workspace",
        "workspaces",
        "catalog",
        "catalogs",
        "peerDependenciesMeta",
    }
)


def _load_json(content: bytes | None) -> dict[str, Any]:
    if content is None:
        return {}

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise DependencyScopeError("duplicate JSON key is ambiguous")
            result[key] = item
        return result

    value = json.loads(content.decode("utf-8"), object_pairs_hook=reject_duplicates)
    if not isinstance(value, dict):
        raise DependencyScopeError("package manifest root is not an object")
    return value


def _load_toml(content: bytes | None) -> dict[str, Any]:
    if content is None:
        return {}
    value = tomllib.loads(content.decode("utf-8"))
    if not isinstance(value, dict):
        raise DependencyScopeError("Python manifest root is not a table")
    return value


def _entries(path: str, content: bytes | None) -> tuple[dict[tuple[str, str], str], dict[str, Any]]:
    if path.endswith("package.json"):
        value = _load_json(content)
        entries: dict[tuple[str, str], str] = {}
        for group in _NODE_GROUPS:
            table = value.get(group, {})
            if table is None:
                continue
            if not isinstance(table, dict) or any(
                not isinstance(key, str) or not isinstance(item, str) for key, item in table.items()
            ):
                raise DependencyScopeError("Node dependency section is ambiguous")
            entries.update({(group, key): item for key, item in table.items()})
        unsupported = {
            key: value[key] for key in _UNSUPPORTED_NODE_KEYS if value.get(key) is not None
        }
        return entries, unsupported

    value = _load_toml(content)
    project = value.get("project", {})
    build = value.get("build-system", {})
    if project is None:
        project = {}
    if build is None:
        build = {}
    if not isinstance(project, dict) or not isinstance(build, dict):
        raise DependencyScopeError("Python dependency tables are ambiguous")
    entries = {}
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise DependencyScopeError("project.dependencies is ambiguous")
    for requirement in dependencies:
        if not isinstance(requirement, str):
            raise DependencyScopeError("project dependency is not a string")
        try:
            parsed_requirement = Requirement(requirement)
        except InvalidRequirement:
            raise DependencyScopeError("project dependency name is ambiguous")
        key = canonicalize_name(parsed_requirement.name)
        if ("project.dependencies", key) in entries:
            raise DependencyScopeError("duplicate project dependency is ambiguous")
        entries[("project.dependencies", key)] = requirement
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise DependencyScopeError("optional dependency groups are ambiguous")
    for group, requirements in optional.items():
        if not isinstance(group, str) or not isinstance(requirements, list):
            raise DependencyScopeError("optional dependency group is ambiguous")
        for requirement in requirements:
            if not isinstance(requirement, str):
                raise DependencyScopeError("optional dependency is not a string")
            try:
                parsed_requirement = Requirement(requirement)
            except InvalidRequirement:
                raise DependencyScopeError("optional dependency name is ambiguous")
            key = canonicalize_name(parsed_requirement.name)
            section = f"project.optional-dependencies:{group}"
            if (section, key) in entries:
                raise DependencyScopeError("duplicate optional dependency is ambiguous")
            entries[(section, key)] = requirement
    requires = build.get("requires", [])
    if not isinstance(requires, list):
        raise DependencyScopeError("build-system.requires is ambiguous")
    for requirement in requires:
        if not isinstance(requirement, str):
            raise DependencyScopeError("build dependency is not a string")
        try:
            parsed_requirement = Requirement(requirement)
        except InvalidRequirement:
            raise DependencyScopeError("build dependency name is ambiguous")
        key = canonicalize_name(parsed_requirement.name)
        if ("build-system.requires", key) in entries:
            raise DependencyScopeError("duplicate build dependency is ambiguous")
        entries[("build-system.requires", key)] = requirement
    unsupported = {
        key: value[key] for key in ("tool", "dependency-groups") if value.get(key) is not None
    }
    for build_key in ("build-backend", "backend-path"):
        if build_key in build:
            unsupported[f"build-system.{build_key}"] = build[build_key]
    dynamic = project.get("dynamic", [])
    if isinstance(dynamic, list) and any(
        item in {"dependencies", "optional-dependencies"} for item in dynamic
    ):
        unsupported["project.dynamic"] = dynamic
    elif dynamic not in ([], None):
        raise DependencyScopeError("project.dynamic is ambiguous")
    return entries, unsupported


def dependency_deltas(snapshot: GitCandidateFile) -> tuple[DependencyDelta, ...]:
    """Return exact supported manifest entry changes for one candidate file."""

    path = normalize_policy_path(snapshot.path)
    if path.rsplit("/", 1)[-1] not in {"package.json", "pyproject.toml"}:
        raise DependencyScopeError("dependency manifest format is unsupported")
    before, before_unknown = _entries(path, snapshot.base_content)
    after, after_unknown = _entries(path, snapshot.head_content)
    if json.dumps(before_unknown, sort_keys=True, separators=(",", ":")) != json.dumps(
        after_unknown, sort_keys=True, separators=(",", ":")
    ):
        raise DependencyScopeError("unsupported dependency-bearing manifest data changed")
    deltas: list[DependencyDelta] = []
    for group, package in sorted(set(before) | set(after)):
        old, new = before.get((group, package)), after.get((group, package))
        if old != new:
            deltas.append(DependencyDelta(path, group, package, old, new))
    return tuple(deltas)


def verify_dependency_declarations(
    deltas: tuple[DependencyDelta, ...], declarations: tuple[str, ...]
) -> str | None:
    """Return an intervention reason, or ``None`` for an exact authorization."""

    expected = {delta.declaration() for delta in deltas}
    actual: set[str] = set()
    for declaration in declarations:
        if not declaration.startswith("dependency:v1:"):
            return "dependency_authorization_is_ambiguous"
        payload = declaration.removeprefix("dependency:v1:")
        try:
            parsed = _load_json(payload.encode("utf-8"))
        except TypeError, ValueError, UnicodeError, json.JSONDecodeError, DependencyScopeError:
            return "dependency_authorization_is_ambiguous"
        if not isinstance(parsed, dict) or set(parsed) != {
            "after",
            "before",
            "group",
            "package",
            "path",
        }:
            return "dependency_authorization_is_ambiguous"
        canonical = "dependency:v1:" + json.dumps(
            parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if canonical != declaration or any(
            not isinstance(parsed[key], (str, type(None))) for key in parsed
        ):
            return "dependency_authorization_is_ambiguous"
        actual.add(canonical)
    if len(actual) != len(declarations):
        return "dependency_authorization_is_ambiguous"
    if actual != expected:
        return "undeclared_dependency_change"
    return None


__all__ = [
    "DependencyDelta",
    "DependencyScopeError",
    "dependency_deltas",
    "verify_dependency_declarations",
]
