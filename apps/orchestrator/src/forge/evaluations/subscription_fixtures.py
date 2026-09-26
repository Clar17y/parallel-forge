"""Frozen v0.2 acceptance repository builders and deterministic independent graders."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.domain.actor import AgentRole
from forge.domain.paths import normalize_policy_path
from forge.domain.policy import CommandSpec, StepKind
from forge.evaluations.contracts import EvaluationCaseContract
from forge.evaluations.credentials import assert_credential_free
from forge.evaluations.errors import (
    MaterializationError,
    UnsafeFixturePathError,
)

FIXTURE_VERSION = "v0.2-acceptance-2"
REPORT_PREFIX = "FORGE_EVAL_REPORT_V1:"
_FIXED_GIT_DATE = "2026-01-01T00:00:00Z"
_GIT_ENV: dict[str, str] = {
    "GIT_AUTHOR_NAME": "Forge fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": _FIXED_GIT_DATE,
    "GIT_COMMITTER_NAME": "Forge fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
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

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_symlink_or_reparse(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise UnsafeFixturePathError(f"cannot inspect path: {path.name}") from exc
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _remove_readonly(func: Any, path: str, exc_info: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _run_git_command(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    clean_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    clean_env.update(_GIT_ENV)
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


@dataclass(frozen=True)
class SubscriptionFixtureManifest:
    """Immutable digest manifest of source, check policy, and fixture version."""

    fixture_version: str
    fixture_name: str
    base_commit: str
    content_digest: str
    files_digest: dict[str, str]
    check_commands: tuple[CommandSpec, ...]
    policy_digest: str
    manifest_digest: str


@dataclass
class SubscriptionFixture:
    """A disposable, isolated acceptance fixture repository."""

    path: Path
    fixture_name: str
    manifest: SubscriptionFixtureManifest
    case_contract: EvaluationCaseContract
    _temporary: bool = False
    _preserve: bool = False

    def preserve(self) -> None:
        """Keep this temporary fixture for inspection rather than deleting on exit."""
        self._preserve = True

    def release(self) -> None:
        """Allow normal cleanup of this fixture."""
        self._preserve = False


def get_acceptance_command_specs() -> tuple[CommandSpec, ...]:
    """Frozen project policy check commands exposed to agents."""
    return (
        CommandSpec(
            kind=StepKind.TEST,
            name="unit",
            argv=("python", "-I", "-B", "tests/fixture_checks.py"),
            timeout_seconds=30,
            required=True,
            network_enabled=False,
        ),
        CommandSpec(
            kind=StepKind.TEST,
            name="slow-unit",
            argv=(
                "python",
                "-I",
                "-B",
                "tests/fixture_checks.py",
                "--barrier-file",
                ".forge-acceptance/slow-unit.release",
            ),
            timeout_seconds=30,
            required=True,
            network_enabled=False,
        ),
    )


def clean_slow_unit_markers(repo_path: Path) -> None:
    """Prepare disposable synchronization state for the UID 10001 check."""
    barrier_dir = repo_path / ".forge-acceptance"
    barrier_dir.mkdir(exist_ok=True)
    if os.name == "posix":
        # Only fixture synchronization lives here; mutation storage stays private.
        barrier_dir.chmod(0o1777)
    for name in ("slow-unit.entered", "slow-unit.release"):
        marker = barrier_dir / name
        marker.unlink(missing_ok=True)


def wait_slow_unit_entered(repo_path: Path, timeout_seconds: float = 10.0) -> bool:
    """Harness helper to observe atomic entrance of slow-unit barrier."""
    marker = repo_path / ".forge-acceptance" / "slow-unit.entered"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if marker.exists():
            try:
                if marker.stat().st_size > 0:
                    return True
            except OSError:
                pass
        time.sleep(0.02)
    return False


def release_slow_unit_barrier(repo_path: Path) -> None:
    """Harness helper to atomically release slow-unit barrier."""
    barrier_dir = repo_path / ".forge-acceptance"
    barrier_dir.mkdir(parents=True, exist_ok=True)
    release_path = barrier_dir / "slow-unit.release"
    tmp_path = barrier_dir / f"slow-unit.release.tmp.{os.getpid()}"
    tmp_path.write_bytes(b"release\n")
    os.replace(tmp_path, release_path)


def _prepare_destination(
    destination: Path | None,
    fixture_name: str,
) -> tuple[Path, bool]:
    """Prepare destination directory; never overwrite or use canonical repository root."""
    if destination is None:
        target_str = tempfile.mkdtemp(prefix=f"forge-acceptance-{fixture_name}-")
        return Path(target_str).resolve(), True

    dest_path = destination.resolve() if not destination.is_absolute() else destination
    current_cwd = Path.cwd().resolve()

    if dest_path == current_cwd or dest_path in current_cwd.parents:
        raise UnsafeFixturePathError(
            f"destination overlaps canonical or parent repository: {dest_path}"
        )
    if (dest_path / "AGENTS.md").exists() and (dest_path / "pyproject.toml").exists():
        raise UnsafeFixturePathError(f"destination is canonical repository root: {dest_path}")

    if dest_path.exists() or os.path.lexists(dest_path):
        if dest_path.is_file():
            raise UnsafeFixturePathError(f"destination already exists as a file: {dest_path}")
        if (dest_path / ".git").exists():
            raise UnsafeFixturePathError(
                f"destination already contains existing .git repository: {dest_path}"
            )
        try:
            if any(dest_path.iterdir()):
                raise UnsafeFixturePathError(
                    f"destination directory already exists and is not empty: {dest_path}"
                )
        except OSError as exc:
            raise UnsafeFixturePathError(f"cannot inspect destination directory: {exc}") from exc
        return dest_path.resolve(), False

    parent = dest_path.parent
    if not parent.exists() or not parent.is_dir():
        raise UnsafeFixturePathError(f"destination parent directory does not exist: {parent}")
    if _is_symlink_or_reparse(parent):
        raise UnsafeFixturePathError(f"destination parent is a symlink or reparse point: {parent}")

    try:
        os.mkdir(dest_path)
    except OSError as exc:
        raise UnsafeFixturePathError(f"failed to create destination directory: {exc}") from exc

    return dest_path.resolve(), False


_COUNTER_GRADER_SOURCE = '"""Immutable acceptance grader for counter-service fixture."""\n\nfrom __future__ import annotations\n\nimport argparse\nimport ast\nimport json\nimport os\nimport sys\nimport time\nfrom pathlib import Path\n\nREPORT_PREFIX = "FORGE_EVAL_REPORT_V1:"\nFIXTURE_VERSION = "v0.2-acceptance-2"\nCASE_KEY = "counter-service"\n\n\ndef _handle_barrier(barrier_path_str: str | None) -> str:\n    if not barrier_path_str:\n        return "unit"\n    barrier_file = Path(barrier_path_str)\n    barrier_dir = barrier_file.parent\n    barrier_dir.mkdir(parents=True, exist_ok=True)\n    entered_marker = barrier_dir / "slow-unit.entered"\n    tmp_marker = barrier_dir / f"slow-unit.entered.tmp.{os.getpid()}"\n    tmp_marker.write_bytes(b"entered\\n")\n    os.replace(tmp_marker, entered_marker)\n\n    deadline = time.monotonic() + 15.0\n    while time.monotonic() < deadline:\n        if barrier_file.exists():\n            return "slow-unit"\n        time.sleep(0.02)\n    raise TimeoutError("Timed out waiting for slow-unit release barrier file")\n\n\ndef _eval_counter(counter_path: Path) -> tuple[bool, bool]:\n    if not counter_path.is_file():\n        return False, False\n    source = counter_path.read_text(encoding="utf-8")\n    try:\n        tree = ast.parse(source, filename=str(counter_path))\n    except SyntaxError:\n        return False, False\n\n    increment_func = None\n    for node in tree.body:\n        if isinstance(node, ast.FunctionDef) and node.name == "increment":\n            increment_func = node\n            break\n    if increment_func is None:\n        return False, False\n\n    safe_mod = ast.fix_missing_locations(ast.Module(body=[increment_func], type_ignores=[]))\n    namespace: dict[str, object] = {"__builtins__": {}}\n    try:\n        exec(compile(safe_mod, str(counter_path), "exec"), namespace)\n        fn = namespace.get("increment")\n        if not callable(fn):\n            return False, False\n        adds_one = bool(fn(0) == 1 and fn(1) == 2 and fn(10) == 11)\n        boundary_fn_ok = bool(fn(-1) == 0 and fn(-2) == -1)\n        return adds_one, boundary_fn_ok\n    except Exception:\n        return False, False\n\n\ndef _eval_test_counter(test_counter_path: Path) -> bool:\n    if not test_counter_path.is_file():\n        return False\n    source = test_counter_path.read_text(encoding="utf-8")\n    try:\n        tree = ast.parse(source, filename=str(test_counter_path))\n    except SyntaxError:\n        return False\n\n    has_negative_one = False\n    for node in ast.walk(tree):\n        if isinstance(node, ast.Assert):\n            for sub in ast.walk(node):\n                if isinstance(sub, ast.Constant) and sub.value == -1:\n                    has_negative_one = True\n                    break\n                if (\n                    isinstance(sub, ast.UnaryOp)\n                    and isinstance(sub.op, ast.USub)\n                    and isinstance(sub.operand, ast.Constant)\n                    and sub.operand.value == 1\n                ):\n                    has_negative_one = True\n                    break\n            if has_negative_one:\n                break\n    return has_negative_one\n\n\ndef _eval_format(format_path: Path, test_format_path: Path) -> bool:\n    if not format_path.is_file() or not test_format_path.is_file():\n        return False\n    source = format_path.read_text(encoding="utf-8")\n    try:\n        tree = ast.parse(source, filename=str(format_path))\n        format_func = None\n        for node in tree.body:\n            if isinstance(node, ast.FunctionDef) and node.name == "format_count":\n                format_func = node\n                break\n        if format_func is None:\n            return False\n        safe_mod = ast.fix_missing_locations(ast.Module(body=[format_func], type_ignores=[]))\n        namespace: dict[str, object] = {"__builtins__": {}}\n        exec(compile(safe_mod, str(format_path), "exec"), namespace)\n        fn = namespace.get("format_count")\n        if not callable(fn):\n            return False\n        return bool(fn(0) == "Count: 0" and fn(42) == "Count: 42")\n    except Exception:\n        return False\n\n\ndef main() -> None:\n    parser = argparse.ArgumentParser()\n    parser.add_argument("--barrier-file", type=str, default=None)\n    args = parser.parse_args()\n\n    command_name = _handle_barrier(args.barrier_file)\n    root = Path(__file__).resolve().parent.parent\n\n    adds_one, boundary_fn_ok = _eval_counter(root / "src" / "counter.py")\n    boundary_test_ok = _eval_test_counter(root / "tests" / "test_counter.py")\n    format_ok = _eval_format(root / "src" / "format.py", root / "tests" / "test_format.py")\n\n    boundary_negative_one = bool(adds_one and boundary_fn_ok and boundary_test_ok)\n\n    tests = {\n        "tests/test_counter.py": bool(adds_one and boundary_negative_one),\n        "tests/test_format.py": bool(format_ok),\n    }\n    assertions = {\n        "increment_adds_one": bool(adds_one),\n        "boundary_negative_one": bool(boundary_negative_one),\n        "format_preserved": bool(format_ok),\n    }\n\n    report = {\n        "report_version": 1,\n        "fixture_version": FIXTURE_VERSION,\n        "case_key": CASE_KEY,\n        "command_name": command_name,\n        "tests": tests,\n        "assertions": assertions,\n    }\n    print(REPORT_PREFIX + json.dumps(report, separators=(",", ":")))\n\n    if all(tests.values()) and all(assertions.values()):\n        sys.exit(0)\n    sys.exit(1)\n\n\nif __name__ == "__main__":\n    main()\n'

_SPLIT_CATALOG_GRADER_SOURCE = '"""Immutable acceptance grader for split-catalog fixture."""\n\nfrom __future__ import annotations\n\nimport argparse\nimport json\nimport os\nimport sys\nimport time\nfrom pathlib import Path\n\nREPORT_PREFIX = "FORGE_EVAL_REPORT_V1:"\nFIXTURE_VERSION = "v0.2-acceptance-2"\nCASE_KEY = "split-catalog"\n\n\ndef _handle_barrier(barrier_path_str: str | None) -> str:\n    if not barrier_path_str:\n        return "unit"\n    barrier_file = Path(barrier_path_str)\n    barrier_dir = barrier_file.parent\n    barrier_dir.mkdir(parents=True, exist_ok=True)\n    entered_marker = barrier_dir / "slow-unit.entered"\n    tmp_marker = barrier_dir / f"slow-unit.entered.tmp.{os.getpid()}"\n    tmp_marker.write_bytes(b"entered\\n")\n    os.replace(tmp_marker, entered_marker)\n\n    deadline = time.monotonic() + 15.0\n    while time.monotonic() < deadline:\n        if barrier_file.exists():\n            return "slow-unit"\n        time.sleep(0.02)\n    raise TimeoutError("Timed out waiting for slow-unit release barrier file")\n\n\ndef main() -> None:\n    parser = argparse.ArgumentParser()\n    parser.add_argument("--barrier-file", type=str, default=None)\n    args = parser.parse_args()\n\n    command_name = _handle_barrier(args.barrier_file)\n    root = Path(__file__).resolve().parent.parent\n\n    alpha_file = root / "alpha" / "value.txt"\n    beta_file = root / "beta" / "value.txt"\n\n    alpha_valid = False\n    if alpha_file.is_file():\n        alpha_text = alpha_file.read_text(encoding="utf-8").strip()\n        alpha_valid = (\n            alpha_text in ("old", "alpha-v2")\n            and "beta" not in alpha_text\n        )\n\n    beta_valid = False\n    if beta_file.is_file():\n        beta_text = beta_file.read_text(encoding="utf-8").strip()\n        beta_valid = (\n            beta_text in ("old", "beta-v2")\n            and "alpha" not in beta_text\n        )\n\n    alpha_dir = root / "alpha"\n    beta_dir = root / "beta"\n    alpha_dir_clean = (\n        alpha_dir.is_dir()\n        and set(p.name for p in alpha_dir.iterdir() if not p.name.startswith(".")) == {"value.txt"}\n    )\n    beta_dir_clean = (\n        beta_dir.is_dir()\n        and set(p.name for p in beta_dir.iterdir() if not p.name.startswith(".")) == {"value.txt"}\n    )\n\n    catalog_halves_isolated = bool(alpha_valid and beta_valid and alpha_dir_clean and beta_dir_clean)\n\n    tests = {\n        "alpha/value.txt": bool(alpha_valid),\n        "beta/value.txt": bool(beta_valid),\n    }\n    assertions = {\n        "alpha_valid": bool(alpha_valid),\n        "beta_valid": bool(beta_valid),\n        "catalog_halves_isolated": bool(catalog_halves_isolated),\n    }\n\n    report = {\n        "report_version": 1,\n        "fixture_version": FIXTURE_VERSION,\n        "case_key": CASE_KEY,\n        "command_name": command_name,\n        "tests": tests,\n        "assertions": assertions,\n    }\n    print(REPORT_PREFIX + json.dumps(report, separators=(",", ":")))\n\n    if catalog_halves_isolated:\n        sys.exit(0)\n    sys.exit(1)\n\n\nif __name__ == "__main__":\n    main()\n'

_REVIEW_GATES_GRADER_SOURCE = '"""Immutable acceptance grader for review-gates fixture."""\n\nfrom __future__ import annotations\n\nimport argparse\nimport ast\nimport json\nimport os\nimport sys\nimport time\nfrom pathlib import Path\n\nREPORT_PREFIX = "FORGE_EVAL_REPORT_V1:"\nFIXTURE_VERSION = "v0.2-acceptance-2"\nCASE_KEY = "review-gates"\n\n\ndef _handle_barrier(barrier_path_str: str | None) -> str:\n    if not barrier_path_str:\n        return "unit"\n    barrier_file = Path(barrier_path_str)\n    barrier_dir = barrier_file.parent\n    barrier_dir.mkdir(parents=True, exist_ok=True)\n    entered_marker = barrier_dir / "slow-unit.entered"\n    tmp_marker = barrier_dir / f"slow-unit.entered.tmp.{os.getpid()}"\n    tmp_marker.write_bytes(b"entered\\n")\n    os.replace(tmp_marker, entered_marker)\n\n    deadline = time.monotonic() + 15.0\n    while time.monotonic() < deadline:\n        if barrier_file.exists():\n            return "slow-unit"\n        time.sleep(0.02)\n    raise TimeoutError("Timed out waiting for slow-unit release barrier file")\n\n\ndef _eval_range_impl(range_path: Path) -> tuple[bool, bool, bool, bool]:\n    if not range_path.is_file():\n        return False, False, False, False\n    source = range_path.read_text(encoding="utf-8")\n    try:\n        tree = ast.parse(source, filename=str(range_path))\n    except SyntaxError:\n        return False, False, False, False\n\n    func = None\n    for node in tree.body:\n        if isinstance(node, ast.FunctionDef) and node.name == "inclusive_range":\n            func = node\n            break\n    if func is None:\n        return False, False, False, False\n\n    safe_mod = ast.fix_missing_locations(ast.Module(body=[func], type_ignores=[]))\n    namespace: dict[str, object] = {\n        "__builtins__": {\n            "ValueError": ValueError,\n            "list": list,\n            "range": range,\n            "len": len,\n        }\n    }\n    try:\n        exec(compile(safe_mod, str(range_path), "exec"), namespace)\n        fn = namespace.get("inclusive_range")\n        if not callable(fn):\n            return False, False, False, False\n        r1 = fn(1, 5)\n        lower_ok = bool(r1 and r1[0] == 1)\n        upper_ok = bool(r1 and r1[-1] == 5 and 5 in r1 and list(r1) == [1, 2, 3, 4, 5])\n        r_rev = fn(5, 1, -1)\n        reversed_ok = bool(list(r_rev) == [5, 4, 3, 2, 1])\n        r_eq = fn(3, 3)\n        equal_ok = bool(list(r_eq) == [3])\n        return lower_ok, upper_ok, reversed_ok, equal_ok\n    except Exception:\n        return False, False, False, False\n\n\ndef _eval_test_range(test_range_path: Path) -> tuple[bool, bool, bool, bool]:\n    if not test_range_path.is_file():\n        return False, False, False, False\n    source = test_range_path.read_text(encoding="utf-8")\n    try:\n        tree = ast.parse(source, filename=str(test_range_path))\n    except SyntaxError:\n        return False, False, False, False\n\n    lower_tested = False\n    upper_tested = False\n    reversed_tested = False\n    equal_tested = False\n\n    for node in ast.walk(tree):\n        if isinstance(node, ast.Call):\n            func_name = getattr(node.func, "id", None)\n            if func_name == "inclusive_range":\n                args = node.args\n                if len(args) >= 2:\n                    if isinstance(args[0], ast.Constant) and isinstance(args[1], ast.Constant):\n                        if args[0].value == args[1].value:\n                            equal_tested = True\n                        if args[0].value > args[1].value:\n                            reversed_tested = True\n                if len(args) >= 3:\n                    if isinstance(args[2], ast.UnaryOp) and isinstance(args[2].op, ast.USub):\n                        reversed_tested = True\n                    elif isinstance(args[2], ast.Constant) and isinstance(args[2].value, int) and args[2].value < 0:\n                        reversed_tested = True\n\n        if isinstance(node, ast.Assert):\n            for sub in ast.walk(node):\n                if isinstance(sub, ast.Constant) and sub.value == 5:\n                    upper_tested = True\n                if isinstance(sub, ast.Constant) and sub.value == 1:\n                    lower_tested = True\n\n    return lower_tested, upper_tested, reversed_tested, equal_tested\n\n\ndef _eval_typo(doc_path: Path) -> tuple[bool, bool]:\n    if not doc_path.is_file():\n        return False, False\n    text = doc_path.read_text(encoding="utf-8")\n    has_the = "the result" in text\n    has_teh = "teh result" in text\n    return has_the, has_teh\n\n\ndef main() -> None:\n    parser = argparse.ArgumentParser()\n    parser.add_argument("--barrier-file", type=str, default=None)\n    args = parser.parse_args()\n\n    command_name = _handle_barrier(args.barrier_file)\n    root = Path(__file__).resolve().parent.parent\n\n    has_the, has_teh = _eval_typo(root / "docs" / "result.md")\n    typo_fixed = bool(has_the and not has_teh)\n\n    lower_ok, upper_ok, reversed_ok, equal_ok = _eval_range_impl(root / "src" / "range.py")\n    inclusive_contract = bool(lower_ok and upper_ok and reversed_ok and equal_ok)\n\n    lower_t, upper_t, reversed_t, equal_t = _eval_test_range(root / "tests" / "test_range.py")\n    boundary_cases = bool(lower_t and upper_t and reversed_t and equal_t)\n\n    if typo_fixed and not inclusive_contract:\n        report = {\n            "report_version": 1,\n            "fixture_version": FIXTURE_VERSION,\n            "case_key": CASE_KEY,\n            "command_name": command_name,\n            "tests": {"docs/result.md": True},\n            "assertions": {"typo_fixed": True},\n        }\n        print(REPORT_PREFIX + json.dumps(report, separators=(",", ":")))\n        sys.exit(0)\n\n    if inclusive_contract and boundary_cases:\n        report = {\n            "report_version": 1,\n            "fixture_version": FIXTURE_VERSION,\n            "case_key": CASE_KEY,\n            "command_name": command_name,\n            "tests": {"tests/test_range.py": True},\n            "assertions": {\n                "inclusive_contract": True,\n                "boundary_cases": True,\n                "equal_bound_tested": True,\n            },\n        }\n        print(REPORT_PREFIX + json.dumps(report, separators=(",", ":")))\n        sys.exit(0)\n\n    if inclusive_contract and not boundary_cases:\n        report = {\n            "report_version": 1,\n            "fixture_version": FIXTURE_VERSION,\n            "case_key": CASE_KEY,\n            "command_name": command_name,\n            "tests": {"tests/test_range.py": False},\n            "assertions": {\n                "inclusive_contract": True,\n                "boundary_cases": False,\n                "equal_bound_tested": bool(equal_t),\n            },\n        }\n        print(REPORT_PREFIX + json.dumps(report, separators=(",", ":")))\n        sys.exit(1)\n\n    report = {\n        "report_version": 1,\n        "fixture_version": FIXTURE_VERSION,\n        "case_key": CASE_KEY,\n        "command_name": command_name,\n        "tests": {\n            "tests/test_range.py": False,\n            "docs/result.md": False,\n        },\n        "assertions": {\n            "inclusive_contract": False,\n            "boundary_cases": False,\n            "typo_fixed": False,\n        },\n    }\n    print(REPORT_PREFIX + json.dumps(report, separators=(",", ":")))\n    sys.exit(1)\n\n\nif __name__ == "__main__":\n    main()\n'


def _calculate_manifest(
    fixture_name: str,
    base_commit: str,
    files: Mapping[str, str],
    commands: tuple[CommandSpec, ...],
) -> SubscriptionFixtureManifest:
    """Calculate cryptographic bindings for source files, check policy, and fixture version."""
    files_digest: dict[str, str] = {}
    content_hasher = hashlib.sha256()

    for rel_path in sorted(files.keys()):
        content_bytes = files[rel_path].encode("utf-8")
        assert_credential_free(files[rel_path], f"fixture file {rel_path}")
        file_sha = hashlib.sha256(content_bytes).hexdigest()
        files_digest[rel_path] = file_sha
        content_hasher.update(f"{rel_path}:{len(content_bytes)}:{file_sha};".encode())

    content_digest = content_hasher.hexdigest()

    policy_dict = [cmd.model_dump(mode="json") for cmd in sorted(commands, key=lambda c: c.name)]
    policy_json = json.dumps(policy_dict, sort_keys=True, separators=(",", ":"))
    policy_digest = hashlib.sha256(policy_json.encode()).hexdigest()

    manifest_hasher = hashlib.sha256()
    manifest_hasher.update(
        f"{FIXTURE_VERSION}:{fixture_name}:{base_commit}:{content_digest}:{policy_digest}".encode()
    )
    manifest_digest = manifest_hasher.hexdigest()

    return SubscriptionFixtureManifest(
        fixture_version=FIXTURE_VERSION,
        fixture_name=fixture_name,
        base_commit=base_commit,
        content_digest=content_digest,
        files_digest=files_digest,
        check_commands=commands,
        policy_digest=policy_digest,
        manifest_digest=manifest_digest,
    )


def _build_git_fixture(
    target_path: Path,
    fixture_name: str,
    files: Mapping[str, str],
    case_contract: EvaluationCaseContract,
    temporary: bool,
) -> SubscriptionFixture:
    """Populate repository files, initialize git with clean identity, and record manifest."""
    for rel_path, text_content in files.items():
        norm_rel = normalize_policy_path(rel_path)
        dest_file = target_path / norm_rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(text_content, encoding="utf-8")

    _run_git_command(["git", "init", "-b", "main", "."], target_path)
    _run_git_command(["git", "add", "--force", "-A"], target_path)
    _run_git_command(
        [
            "git",
            "-c",
            f"user.name={_GIT_ENV['GIT_AUTHOR_NAME']}",
            "-c",
            f"user.email={_GIT_ENV['GIT_AUTHOR_EMAIL']}",
            "commit",
            "-m",
            f"forge: {fixture_name} v0.2 baseline",
        ],
        target_path,
    )

    commit_res = _run_git_command(["git", "rev-parse", "HEAD"], target_path)
    base_commit = commit_res.stdout.strip()

    commands = get_acceptance_command_specs()
    manifest = _calculate_manifest(fixture_name, base_commit, files, commands)

    return SubscriptionFixture(
        path=target_path,
        fixture_name=fixture_name,
        manifest=manifest,
        case_contract=case_contract,
        _temporary=temporary,
        _preserve=False,
    )


def get_counter_service_case(base_dir: Path | None = None) -> EvaluationCaseContract:
    """Return frozen EvaluationCaseContract for counter-service fixture."""
    return EvaluationCaseContract(
        fixture_version=FIXTURE_VERSION,
        case_key="counter-service",
        task=(
            "Make increment add one; preserve formatting behavior; "
            "add a boundary assertion for -1; change only src/counter.py and tests/test_counter.py."
        ),
        role=AgentRole.DEVELOPER,
        allowed_paths=("src/counter.py", "tests/test_counter.py"),
        required_tests=("tests/test_counter.py", "tests/test_format.py"),
        required_checks=("unit", "slow-unit"),
        check_commands=get_acceptance_command_specs(),
        required_assertions=(
            "increment_adds_one",
            "boundary_negative_one",
            "format_preserved",
        ),
        base_directory=base_dir,
    )


def get_split_catalog_alpha_case(base_dir: Path | None = None) -> EvaluationCaseContract:
    """Return frozen EvaluationCaseContract for split-catalog alpha task."""
    return EvaluationCaseContract(
        fixture_version=FIXTURE_VERSION,
        case_key="split-catalog",
        task="Set alpha/value.txt to alpha-v2; change only alpha/value.txt.",
        role=AgentRole.DEVELOPER,
        allowed_paths=("alpha/value.txt",),
        required_tests=("alpha/value.txt", "beta/value.txt"),
        required_checks=("unit", "slow-unit"),
        check_commands=get_acceptance_command_specs(),
        required_assertions=("alpha_valid", "beta_valid", "catalog_halves_isolated"),
        base_directory=base_dir,
    )


def get_split_catalog_beta_case(base_dir: Path | None = None) -> EvaluationCaseContract:
    """Return frozen EvaluationCaseContract for split-catalog beta task."""
    return EvaluationCaseContract(
        fixture_version=FIXTURE_VERSION,
        case_key="split-catalog",
        task="Set beta/value.txt to beta-v2; change only beta/value.txt.",
        role=AgentRole.DEVELOPER,
        allowed_paths=("beta/value.txt",),
        required_tests=("alpha/value.txt", "beta/value.txt"),
        required_checks=("unit", "slow-unit"),
        check_commands=get_acceptance_command_specs(),
        required_assertions=("alpha_valid", "beta_valid", "catalog_halves_isolated"),
        base_directory=base_dir,
    )


def get_review_gates_range_case(base_dir: Path | None = None) -> EvaluationCaseContract:
    """Return frozen EvaluationCaseContract for review-gates range task."""
    return EvaluationCaseContract(
        fixture_version=FIXTURE_VERSION,
        case_key="review-gates",
        task=(
            "Fix inclusive range to include upper bound; cover lower, upper, reversed, "
            "and equal-bound cases; change only src/range.py and tests/test_range.py."
        ),
        role=AgentRole.DEVELOPER,
        allowed_paths=("src/range.py", "tests/test_range.py"),
        required_tests=("tests/test_range.py",),
        required_checks=("unit", "slow-unit"),
        check_commands=get_acceptance_command_specs(),
        required_assertions=("inclusive_contract", "boundary_cases", "equal_bound_tested"),
        base_directory=base_dir,
    )


def get_review_gates_typo_case(base_dir: Path | None = None) -> EvaluationCaseContract:
    """Return frozen EvaluationCaseContract for review-gates typo task."""
    return EvaluationCaseContract(
        fixture_version=FIXTURE_VERSION,
        case_key="review-gates",
        task="Correct exact typo 'teh result' to 'the result'; change only docs/result.md.",
        role=AgentRole.DEVELOPER,
        allowed_paths=("docs/result.md",),
        required_tests=("docs/result.md",),
        required_checks=("unit", "slow-unit"),
        check_commands=get_acceptance_command_specs(),
        required_assertions=("typo_fixed",),
        base_directory=base_dir,
    )


def build_counter_service_fixture(destination: Path | None = None) -> SubscriptionFixture:
    """Build counter-service git fixture at disposable path."""
    dest, temporary = _prepare_destination(destination, "counter-service")
    files = {
        ".gitignore": ".forge/\n.forge-acceptance/\n.worktrees/\n__pycache__/\n*.pyc\n",
        "src/counter.py": (
            '"""Counter service implementation."""\n\n'
            "def increment(value: int) -> int:\n"
            '    """Increment value by one (faulty initial adds two)."""\n'
            "    return value + 2\n"
        ),
        "src/format.py": (
            '"""Display formatting helpers."""\n\n'
            "def format_count(count: int) -> str:\n"
            '    """Format counter value as display label."""\n'
            '    return f"Count: {count}"\n'
        ),
        "tests/test_counter.py": (
            '"""Tests for counter."""\n\n'
            "from counter import increment\n\n"
            "def test_increment() -> None:\n"
            "    assert increment(0) == 1\n"
            "    assert increment(10) == 11\n"
        ),
        "tests/test_format.py": (
            '"""Tests for format."""\n\n'
            "from format import format_count\n\n"
            "def test_format_count() -> None:\n"
            '    assert format_count(0) == "Count: 0"\n'
            '    assert format_count(42) == "Count: 42"\n'
        ),
        "tests/fixture_checks.py": _COUNTER_GRADER_SOURCE,
    }
    case = get_counter_service_case(dest)
    return _build_git_fixture(dest, "counter-service", files, case, temporary)


def build_split_catalog_fixture(
    destination: Path | None = None, fixture_name: str = "split-catalog"
) -> SubscriptionFixture:
    """Build split-catalog git fixture at disposable path."""
    dest, temporary = _prepare_destination(destination, fixture_name)
    files = {
        ".gitignore": ".forge/\n.forge-acceptance/\n.worktrees/\n__pycache__/\n*.pyc\n",
        "README.md": (
            "# Split Catalog\n\nRepository with independent alpha and beta partitions.\n"
        ),
        "alpha/value.txt": "old\n",
        "beta/value.txt": "old\n",
        "tests/fixture_checks.py": _SPLIT_CATALOG_GRADER_SOURCE,
    }
    case = get_split_catalog_alpha_case(dest)
    return _build_git_fixture(dest, fixture_name, files, case, temporary)


def build_catalog_copies(
    destination_a: Path, destination_b: Path
) -> tuple[SubscriptionFixture, SubscriptionFixture]:
    """Build identical independent catalog-A and catalog-B copies for concurrency testing."""
    fixture_a = build_split_catalog_fixture(destination_a, fixture_name="catalog-A")
    fixture_b = build_split_catalog_fixture(destination_b, fixture_name="catalog-B")
    return fixture_a, fixture_b


def build_review_gates_fixture(destination: Path | None = None) -> SubscriptionFixture:
    """Build review-gates git fixture containing range implementation and doc typo."""
    dest, temporary = _prepare_destination(destination, "review-gates")
    files = {
        ".gitignore": ".forge/\n.forge-acceptance/\n.worktrees/\n__pycache__/\n*.pyc\n",
        "src/range.py": (
            '"""Range operations with inclusive boundary contract."""\n\n'
            "def inclusive_range(start: int, stop: int, step: int = 1) -> list[int]:\n"
            '    """Return integers from start to stop inclusive."""\n'
            "    if step == 0:\n"
            '        raise ValueError("step must not be zero")\n'
            "    # Faulty initial implementation: Python range() omits stop\n"
            "    return list(range(start, stop, step))\n"
        ),
        "tests/test_range.py": (
            '"""Tests for inclusive range."""\n\n'
            "from range import inclusive_range\n\n"
            "def test_interior() -> None:\n"
            "    result = inclusive_range(1, 5)\n"
            "    assert 3 in result\n"
        ),
        "docs/result.md": (
            "# Acceptance Result\n\nThe automated run generates teh result for verification.\n"
        ),
        "tests/fixture_checks.py": _REVIEW_GATES_GRADER_SOURCE,
    }
    case = get_review_gates_range_case(dest)
    return _build_git_fixture(dest, "review-gates", files, case, temporary)


@contextlib.contextmanager
def materialize_subscription_fixture(
    name: str,
    destination: Path | None = None,
) -> Iterator[SubscriptionFixture]:
    """Materialize a subscription acceptance fixture in a managed context with auto-cleanup."""
    builders = {
        "counter-service": build_counter_service_fixture,
        "split-catalog": build_split_catalog_fixture,
        "review-gates": build_review_gates_fixture,
    }
    builder = builders.get(name)
    if builder is None:
        raise ValueError(f"unknown subscription fixture name: {name}")

    fixture = builder(destination)
    try:
        yield fixture
    finally:
        if fixture._temporary and not fixture._preserve and fixture.path.exists():
            shutil.rmtree(str(fixture.path), onerror=_remove_readonly)


__all__ = [
    "FIXTURE_VERSION",
    "REPORT_PREFIX",
    "SubscriptionFixture",
    "SubscriptionFixtureManifest",
    "build_catalog_copies",
    "build_counter_service_fixture",
    "build_review_gates_fixture",
    "build_split_catalog_fixture",
    "clean_slow_unit_markers",
    "get_acceptance_command_specs",
    "get_counter_service_case",
    "get_review_gates_range_case",
    "get_review_gates_typo_case",
    "get_split_catalog_alpha_case",
    "get_split_catalog_beta_case",
    "materialize_subscription_fixture",
    "release_slow_unit_barrier",
    "wait_slow_unit_entered",
]
