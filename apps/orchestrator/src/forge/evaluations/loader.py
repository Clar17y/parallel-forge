"""Evaluation case loading and fixture discovery."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from forge.evaluations.contracts import EvaluationCaseContract
from forge.evaluations.credentials import assert_credential_free
from forge.evaluations.errors import (
    FixtureNotFoundError,
    InvalidCaseContractError,
    UnsafeFixturePathError,
)

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MAX_JSON_FILE_SIZE = 1 * 1024 * 1024  # 1 MB max for contract/expected JSON files
_MAX_DISCOVERED_CASES = 100
_MAX_SEARCH_DEPTH = 6


def _is_symlink_or_reparse(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise UnsafeFixturePathError(f"cannot inspect path: {path.name}") from exc
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _read_bounded_json_file(file_path: Path, label: str) -> str:
    """Read a JSON file safely with size bounds, symlink rejection, and credential scanning."""
    if not file_path.exists() or not file_path.is_file():
        raise FixtureNotFoundError(f"{label} file does not exist: {file_path.name}")

    if _is_symlink_or_reparse(file_path):
        raise UnsafeFixturePathError(f"{label} file is a symlink or reparse point: {file_path.name}")

    try:
        st = os.lstat(file_path)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeFixturePathError(f"{label} file is not a regular file: {file_path.name}")
        if st.st_size > _MAX_JSON_FILE_SIZE:
            raise InvalidCaseContractError(f"{label} file exceeds maximum allowed size")

        with file_path.open("r", encoding="utf-8") as f:
            raw_text = f.read(_MAX_JSON_FILE_SIZE + 1)
        if len(raw_text) > _MAX_JSON_FILE_SIZE:
            raise InvalidCaseContractError(f"{label} file exceeds maximum allowed size")
    except (OSError, UnicodeDecodeError) as exc:
        if isinstance(exc, (InvalidCaseContractError, UnsafeFixturePathError)):
            raise
        raise InvalidCaseContractError(f"cannot read {label} file: {type(exc).__name__}") from None

    assert_credential_free(raw_text, f"{label} {file_path.name}")
    return raw_text


def load_evaluation_case(path: Path | str) -> EvaluationCaseContract:
    """Load and validate an evaluation case from a directory or task.json file."""
    case_path = Path(path)
    if not case_path.exists():
        raise FixtureNotFoundError(f"case path does not exist: {case_path.name}")

    if _is_symlink_or_reparse(case_path):
        raise UnsafeFixturePathError(f"case path is a symlink or reparse point: {case_path.name}")

    target_file: Path
    base_dir: Path
    if case_path.is_dir():
        target_file = case_path / "task.json"
        if not target_file.exists():
            target_file = case_path / "case.json"
        if not target_file.exists():
            raise FixtureNotFoundError(f"no task.json or case.json found in {case_path.name}")
        base_dir = case_path
    else:
        target_file = case_path
        base_dir = case_path.parent

    raw_text = _read_bounded_json_file(target_file, "case")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        raise InvalidCaseContractError(f"malformed JSON in case file {target_file.name}") from None

    if not isinstance(data, dict):
        raise InvalidCaseContractError(f"case contract must be a JSON object, got {type(data).__name__}")

    # Default case_key to base directory name if not specified
    if "case_key" not in data:
        data["case_key"] = base_dir.name

    data["base_directory"] = base_dir

    try:
        return EvaluationCaseContract.model_validate(data)
    except ValidationError as exc:
        err_msgs = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            msg = err.get("msg", "validation error")
            err_msgs.append(f"{loc}: {msg}" if loc else msg)
        raise InvalidCaseContractError(
            f"invalid evaluation case contract in {target_file.name}: {'; '.join(err_msgs)}"
        ) from None
    except Exception as exc:
        if isinstance(exc, InvalidCaseContractError):
            raise
        raise InvalidCaseContractError(f"invalid evaluation case contract in {target_file.name}") from None


def load_evaluation_cases(root_directory: Path | str) -> dict[str, EvaluationCaseContract]:
    """Discover and load all evaluation cases under a root directory."""
    root = Path(root_directory)
    if not root.exists() or not root.is_dir():
        raise FixtureNotFoundError(f"evaluation fixtures directory does not exist: {root.name}")

    if _is_symlink_or_reparse(root):
        raise UnsafeFixturePathError(f"evaluation fixtures root is a symlink: {root.name}")

    cases: dict[str, EvaluationCaseContract] = {}
    for root_str, dirnames, filenames in os.walk(str(root), followlinks=False):
        current_dir = Path(root_str)
        if _is_symlink_or_reparse(current_dir):
            dirnames.clear()
            continue

        try:
            rel_depth = len(current_dir.relative_to(root).parts)
        except ValueError:
            dirnames.clear()
            continue

        if rel_depth > _MAX_SEARCH_DEPTH:
            dirnames.clear()
            continue

        safe_dirs: list[str] = []
        for d in dirnames:
            d_path = current_dir / d
            if not _is_symlink_or_reparse(d_path):
                safe_dirs.append(d)
        dirnames[:] = safe_dirs

        if "task.json" in filenames or "case.json" in filenames:
            case_file = current_dir / ("task.json" if "task.json" in filenames else "case.json")
            case = load_evaluation_case(case_file)
            if case.case_key in cases:
                raise InvalidCaseContractError(f"duplicate case_key discovered: {case.case_key}")
            if len(cases) >= _MAX_DISCOVERED_CASES:
                raise InvalidCaseContractError(f"discovery exceeded maximum cases limit of {_MAX_DISCOVERED_CASES}")
            cases[case.case_key] = case

    return cases


def load_expected_output(path: Path | str) -> dict[str, Any]:
    """Load and validate an expected evaluation output JSON file."""
    expected_path = Path(path)
    raw_text = _read_bounded_json_file(expected_path, "expected output")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        raise InvalidCaseContractError(f"malformed JSON in expected output {expected_path.name}") from None

    if not isinstance(data, dict):
        raise InvalidCaseContractError(f"expected output must be a JSON object, got {type(data).__name__}")

    return data


__all__ = [
    "load_evaluation_case",
    "load_evaluation_cases",
    "load_expected_output",
]
