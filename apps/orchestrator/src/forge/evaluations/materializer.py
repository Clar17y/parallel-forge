"""Deterministic, isolated Git repository fixture materializer."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from forge.domain.paths import RESERVED_REPOSITORY_COMPONENTS, normalize_policy_path
from forge.evaluations.contracts import EvaluationCaseContract
from forge.evaluations.credentials import assert_credential_free
from forge.evaluations.errors import (
    MaterializationError,
    UnsafeFixturePathError,
)

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per file
_MAX_FILE_COUNT = 1_000
_MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MB total content

_FIXED_GIT_DATE = "2026-01-01T00:00:00Z"
_GIT_ENV: dict[str, str] = {
    "GIT_AUTHOR_NAME": "Forge Evaluation",
    "GIT_AUTHOR_EMAIL": "evaluation@forge.local",
    "GIT_AUTHOR_DATE": _FIXED_GIT_DATE,
    "GIT_COMMITTER_NAME": "Forge Evaluation",
    "GIT_COMMITTER_EMAIL": "evaluation@forge.local",
    "GIT_COMMITTER_DATE": _FIXED_GIT_DATE,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_COUNT": "0",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_ATTR_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "true",
    "GIT_SSH_COMMAND": "true",
    "GIT_TEMPLATE_DIR": "",
    "GIT_HOOKS_PATH": "",
}

_GIT_BASE_FLAGS: list[str] = [
    "-c",
    "init.templateDir=",
    "-c",
    "core.hooksPath=",
    "-c",
    "core.attributesFile=",
    "-c",
    "init.defaultBranch=main",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.fileMode=false",
    "-c",
    "commit.gpgSign=false",
    "-c",
    "tag.gpgSign=false",
]


class TemplateSnapshotFile(NamedTuple):
    """Immutable in-memory snapshot of a validated template file."""

    rel_path: str
    content: bytes
    size: int
    sha256: str


@dataclass(frozen=True)
class MaterializedFixture:
    """An isolated, materialized evaluation fixture repository."""

    path: Path
    base_commit: str
    fixture_identity: str
    case: EvaluationCaseContract


def _remove_readonly(func: Any, path: str, exc_info: Any) -> None:
    """Error handler for shutil.rmtree on Windows read-only git files."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _is_symlink_or_reparse(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise UnsafeFixturePathError(f"cannot inspect path: {path.name}") from exc
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def validate_repository_template(template_dir: Path) -> list[TemplateSnapshotFile]:
    """Inspect repository template for safety, bounds, credentials, and snapshot validated bytes."""
    if not template_dir.exists() or not template_dir.is_dir():
        raise UnsafeFixturePathError(f"repository template directory does not exist: {template_dir.name}")

    if _is_symlink_or_reparse(template_dir):
        raise UnsafeFixturePathError(f"repository template is a symlink or reparse point: {template_dir.name}")

    files: list[TemplateSnapshotFile] = []
    total_bytes = 0

    # Walk directory structure without following symlinks
    for root_str, dirnames, filenames in os.walk(str(template_dir), followlinks=False):
        root = Path(root_str)

        # Check directory itself
        if _is_symlink_or_reparse(root):
            raise UnsafeFixturePathError(f"template directory component is a symlink: {root.name}")

        # Check for reserved directory components, especially .git
        rel_dir = root.relative_to(template_dir)
        for part in rel_dir.parts:
            part_lower = part.casefold() if os.name == "nt" else part
            if part_lower == ".git":
                raise UnsafeFixturePathError(f"repository template must not contain .git: {root.name}")
            if any(part_lower == reserved.casefold() for reserved in RESERVED_REPOSITORY_COMPONENTS):
                raise UnsafeFixturePathError(f"repository template contains reserved component: {part}")

        # Reject symlink directories
        for d in dirnames:
            dir_path = root / d
            if _is_symlink_or_reparse(dir_path):
                raise UnsafeFixturePathError(f"symlink or reparse directory in template: {dir_path.name}")

        for filename in filenames:
            file_path = root / filename
            if _is_symlink_or_reparse(file_path):
                raise UnsafeFixturePathError(f"symlink or reparse file in template: {file_path.name}")

            try:
                st = os.lstat(file_path)
            except OSError as exc:
                raise UnsafeFixturePathError(f"cannot inspect template file: {filename}") from exc

            # Regular files only
            if not stat.S_ISREG(st.st_mode):
                raise UnsafeFixturePathError(f"template file is not a regular file: {filename}")

            # Compute relative path and normalize
            rel_path = file_path.relative_to(template_dir).as_posix()
            try:
                normalized_rel = normalize_policy_path(rel_path)
            except ValueError as exc:
                raise UnsafeFixturePathError(f"unsafe template file path: {rel_path}: {exc}") from exc

            # Bounded actual read
            try:
                with file_path.open("rb") as f:
                    content = f.read(_MAX_FILE_SIZE + 1)
            except OSError as exc:
                raise UnsafeFixturePathError(f"cannot read template file {normalized_rel}: {exc}") from exc

            if len(content) > _MAX_FILE_SIZE:
                raise UnsafeFixturePathError(
                    f"template file exceeds size limit ({_MAX_FILE_SIZE} bytes): {normalized_rel}"
                )

            total_bytes += len(content)
            if total_bytes > _MAX_TOTAL_BYTES:
                raise UnsafeFixturePathError(f"template total size exceeds limit ({_MAX_TOTAL_BYTES} bytes)")

            # Check credentials in validated bytes
            try:
                text_content = content.decode("utf-8")
                assert_credential_free(text_content, f"template file {normalized_rel}")
            except UnicodeDecodeError:
                ascii_text = content.decode("latin-1", errors="ignore")
                assert_credential_free(ascii_text, f"template binary file {normalized_rel}")

            file_hash = hashlib.sha256(content).hexdigest()
            files.append(
                TemplateSnapshotFile(
                    rel_path=normalized_rel,
                    content=content,
                    size=len(content),
                    sha256=file_hash,
                )
            )

            if len(files) > _MAX_FILE_COUNT:
                raise UnsafeFixturePathError(f"template file count exceeds limit of {_MAX_FILE_COUNT}")

    # Sort files deterministically by relative path
    files.sort(key=lambda item: item.rel_path)
    return files


def calculate_fixture_identity(
    case: EvaluationCaseContract,
    files: Sequence[TemplateSnapshotFile],
) -> str:
    """Compute a deterministic, cryptographic SHA-256 identity for the fixture and case."""
    if len(files) > _MAX_FILE_COUNT:
        raise UnsafeFixturePathError("too many snapshot files")
    seen: set[str] = set()
    total_bytes = 0
    for snapshot in files:
        if type(snapshot) is not TemplateSnapshotFile or type(snapshot.content) is not bytes:
            raise UnsafeFixturePathError("invalid snapshot file")
        try:
            path = normalize_policy_path(snapshot.rel_path)
        except ValueError:
            raise UnsafeFixturePathError("unsafe snapshot path") from None
        key = path.casefold()
        if key in seen:
            raise UnsafeFixturePathError("duplicate snapshot path")
        seen.add(key)
        total_bytes += len(snapshot.content)
        if (type(snapshot.size) is not int or snapshot.size != len(snapshot.content)
                or snapshot.size > _MAX_FILE_SIZE or total_bytes > _MAX_TOTAL_BYTES
                or snapshot.sha256 != hashlib.sha256(snapshot.content).hexdigest()):
            raise UnsafeFixturePathError("snapshot content identity differs")
        assert_credential_free(snapshot.content.decode("latin-1"), "snapshot content")
    hasher = hashlib.sha256()

    # Hash case contract specification
    case_dict = {
        "fixture_version": case.fixture_version,
        "case_key": case.case_key,
        "role": case.role.value,
        "metric_version": case.metric_version,
        "task": case.task,
        "expected_components": sorted(case.expected_components),
        "expected_checks": sorted(case.expected_checks),
        "expected_risks": sorted(case.expected_risks),
        "expected_dependencies": sorted(case.expected_dependencies),
        "expected_defects": {
            k: {
                "severity": d.severity.value,
                "path": d.path,
                "start_line": d.start_line,
                "evidence_anchor": d.evidence_anchor,
                "missing_test": d.missing_test,
            }
            for k, d in sorted(case.expected_defects.items())
        },
        "allowed_paths": sorted(case.allowed_paths),
        "required_tests": sorted(case.required_tests),
        "required_checks": sorted(case.required_checks),
        "required_assertions": sorted(case.required_assertions),
        "prohibited_tools": sorted(case.prohibited_tools),
        "max_cost_minor": case.max_cost_minor,
        "max_duration_ms": case.max_duration_ms,
    }
    canonical_case_json = json.dumps(case_dict, sort_keys=True, separators=(",", ":"))
    hasher.update(b"case:")
    hasher.update(canonical_case_json.encode("utf-8"))

    # Hash repository files
    hasher.update(b":repo:")
    for item in sorted(files, key=lambda item: item.rel_path):
        hasher.update(f"{item.rel_path}:{item.size}:{item.sha256};".encode())

    return hasher.hexdigest()


def _run_git_command(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    # Strip any inherited GIT_* environment variables to prevent hijacking
    clean_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    clean_env.update(_GIT_ENV)

    # Prepend isolation flags
    full_cmd = [argv[0]] + _GIT_BASE_FLAGS + argv[1:]
    try:
        result = subprocess.run(
            full_cmd,
            cwd=str(cwd),
            env=clean_env,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        raise MaterializationError(f"failed to run git command {argv}: {exc}") from exc

    if result.returncode != 0:
        raise MaterializationError(
            f"git command failed (code {result.returncode}): {' '.join(argv)}\n{result.stderr}"
        )
    return result


def _resolve_and_confine_template_dir(case: EvaluationCaseContract) -> Path:
    """Safely validate, confine, and resolve repository template within case base directory."""
    if case.base_directory is None:
        raise UnsafeFixturePathError(f"base directory required to resolve template for case {case.case_key}")

    base = case.base_directory
    if not base.exists() or not base.is_dir():
        raise UnsafeFixturePathError(f"case base directory does not exist: {base}")

    if _is_symlink_or_reparse(base):
        raise UnsafeFixturePathError(f"case base directory is a symlink or reparse point: {base}")

    rel_template = case.repository_template if case.repository_template is not None else "repository"
    try:
        norm_rel = normalize_policy_path(rel_template)
    except ValueError as exc:
        raise UnsafeFixturePathError(f"invalid repository template path: {exc}") from exc

    # Reject symlinks or reparse points at each step before full resolution
    current = base
    for part in norm_rel.split("/"):
        current = current / part
        if not current.exists():
            raise UnsafeFixturePathError(f"repository template component does not exist: {part}")
        if _is_symlink_or_reparse(current):
            raise UnsafeFixturePathError(f"repository template component is a symlink or reparse point: {part}")

    if not current.is_dir():
        raise UnsafeFixturePathError(f"repository template is not a directory: {norm_rel}")

    resolved_base = base.resolve()
    resolved_template = current.resolve()

    try:
        rel = resolved_template.relative_to(resolved_base)
    except ValueError:
        raise UnsafeFixturePathError(f"repository template escapes case base directory: {norm_rel}")

    if len(rel.parts) == 0:
        raise UnsafeFixturePathError("repository template cannot be the case base directory itself")

    return resolved_template


def _prepare_destination(
    destination: Path | None,
    template_dir: Path,
) -> tuple[Path, bool]:
    """Prepare and confine destination directory; never overwrite or follow links."""
    if destination is None:
        target_str = tempfile.mkdtemp(prefix="forge-eval-fixture-")
        return Path(target_str).resolve(), True

    dest_path = destination.resolve() if not destination.is_absolute() else destination
    if dest_path.exists() or os.path.lexists(dest_path):
        raise UnsafeFixturePathError(f"supplied destination already exists: {dest_path}")

    parent = dest_path.parent
    if not parent.exists() or not parent.is_dir():
        raise UnsafeFixturePathError(f"supplied destination parent directory does not exist: {parent}")
    if _is_symlink_or_reparse(parent):
        raise UnsafeFixturePathError(f"supplied destination parent is a symlink or reparse point: {parent}")

    resolved_dest = dest_path.resolve()
    resolved_template = template_dir.resolve()

    if (
        resolved_dest == resolved_template
        or resolved_template in resolved_dest.parents
        or resolved_dest in resolved_template.parents
    ):
        raise UnsafeFixturePathError("destination cannot overlap with template source directory")

    try:
        os.mkdir(resolved_dest)
    except (FileExistsError, OSError) as exc:
        raise UnsafeFixturePathError(f"failed to create destination exclusively: {exc}") from exc

    return resolved_dest, False


@contextlib.contextmanager
def materialize_fixture(
    case: EvaluationCaseContract,
    destination: Path | None = None,
    snapshots: Sequence[TemplateSnapshotFile] | None = None,
) -> Iterator[MaterializedFixture]:
    """Materialize a repository template into an isolated, temporary Git repository."""
    template_dir = _resolve_and_confine_template_dir(case)
    files = list(snapshots) if snapshots is not None else validate_repository_template(template_dir)
    fixture_identity = calculate_fixture_identity(case, files)

    target_path, temp_dir_created = _prepare_destination(destination, template_dir)

    try:
        # Materialize files strictly from snapshotted validated bytes
        for snap in files:
            dest_file = target_path / snap.rel_path
            current = target_path
            for part in Path(snap.rel_path).parent.parts:
                current = current / part
                if not current.exists():
                    current.mkdir()
                if _is_symlink_or_reparse(current):
                    raise UnsafeFixturePathError("destination path component is a symlink")

            if dest_file.exists() or os.path.lexists(dest_file):
                raise UnsafeFixturePathError(f"destination file already exists: {snap.rel_path}")

            dest_file.write_bytes(snap.content)

        # Initialize deterministic, isolated Git repository
        _run_git_command(["git", "init", "."], target_path)
        _run_git_command(["git", "add", "--force", "-A"], target_path)
        _run_git_command(
            [
                "git",
                "-c",
                "user.name=Forge Evaluation",
                "-c",
                "user.email=evaluation@forge.local",
                "commit",
                "-m",
                "evaluation fixture base",
            ],
            target_path,
        )

        commit_result = _run_git_command(["git", "rev-parse", "HEAD"], target_path)
        base_commit = commit_result.stdout.strip()
        if len(base_commit) != 40:
            raise MaterializationError(f"invalid base commit returned: {base_commit}")

        yield MaterializedFixture(
            path=target_path,
            base_commit=base_commit,
            fixture_identity=fixture_identity,
            case=case,
        )
    finally:
        # Only remove temporary directories created by us; never remove caller-owned paths
        if temp_dir_created and target_path.exists():
            shutil.rmtree(str(target_path), onerror=_remove_readonly)


__all__ = [
    "MaterializedFixture",
    "TemplateSnapshotFile",
    "calculate_fixture_identity",
    "materialize_fixture",
    "validate_repository_template",
]
