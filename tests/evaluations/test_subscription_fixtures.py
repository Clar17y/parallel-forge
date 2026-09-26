"""Tests for frozen v0.2 acceptance subscription fixtures and deterministic graders."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.paths import normalize_policy_path
from forge.domain.policy import StepKind
from forge.evaluations.check_evidence import read_check_evidence
from forge.evaluations.errors import UnsafeFixturePathError
from forge.evaluations.materializer import _remove_readonly
from forge.evaluations.subscription_fixtures import (
    FIXTURE_VERSION,
    REPORT_PREFIX,
    build_catalog_copies,
    build_counter_service_fixture,
    build_review_gates_fixture,
    build_split_catalog_fixture,
    clean_slow_unit_markers,
    get_acceptance_command_specs,
    get_counter_service_case,
    get_review_gates_range_case,
    get_review_gates_typo_case,
    get_split_catalog_alpha_case,
    materialize_subscription_fixture,
    release_slow_unit_barrier,
    wait_slow_unit_entered,
)


def _run_cmd(argv: list[str], cwd: Path) -> tuple[int, str, str]:
    res = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return res.returncode, res.stdout, res.stderr


def _parse_report(stdout: str) -> dict[str, Any]:
    lines = [
        line[len(REPORT_PREFIX) :] for line in stdout.splitlines() if line.startswith(REPORT_PREFIX)
    ]
    assert len(lines) == 1, f"Expected exactly one report line, found {len(lines)} in:\n{stdout}"
    parsed: Any = json.loads(lines[0])
    assert isinstance(parsed, dict)
    return parsed


def test_fixture_version_constant() -> None:
    assert FIXTURE_VERSION == "v0.2-acceptance-2"


def test_barrier_state_excludes_private_mutation_storage(tmp_path: Path) -> None:
    release_slow_unit_barrier(tmp_path)
    assert not (tmp_path / ".forge").exists()
    assert (tmp_path / ".forge-acceptance" / "slow-unit.release").is_file()
    with pytest.raises(ValueError):
        normalize_policy_path(".forge-acceptance/slow-unit.release")


def test_acceptance_command_specs() -> None:
    commands = get_acceptance_command_specs()
    assert len(commands) == 2
    cmd_map = {c.name: c for c in commands}
    assert "unit" in cmd_map
    assert "slow-unit" in cmd_map
    assert cmd_map["unit"].argv == ("python", "-I", "-B", "tests/fixture_checks.py")
    assert cmd_map["slow-unit"].argv == (
        "python",
        "-I",
        "-B",
        "tests/fixture_checks.py",
        "--barrier-file",
        ".forge-acceptance/slow-unit.release",
    )
    assert cmd_map["unit"].kind == StepKind.TEST
    assert cmd_map["slow-unit"].kind == StepKind.TEST


def test_counter_service_lifecycle_and_grading(tmp_path: Path) -> None:
    dest = tmp_path / "counter-service"
    fixture = build_counter_service_fixture(dest)
    assert fixture.fixture_name == "counter-service"
    assert fixture.path == dest
    assert fixture.manifest.fixture_version == FIXTURE_VERSION
    assert len(fixture.manifest.base_commit) == 40
    assert (dest / ".git").is_dir()

    # Verify git author and branch
    _ret, out, _err = _run_cmd(["git", "branch", "--show-current"], dest)
    assert out.strip() == "main"
    _ret, out, _err = _run_cmd(["git", "log", "-1", "--format=%an <%ae>"], dest)
    assert out.strip() == "Forge fixture <fixture@example.invalid>"

    # Verify .gitignore
    gitignore = (dest / ".gitignore").read_text(encoding="utf-8")
    assert ".forge/" in gitignore
    assert ".worktrees/" in gitignore

    # Verify required files exist
    assert (dest / "src" / "counter.py").is_file()
    assert (dest / "src" / "format.py").is_file()
    assert (dest / "tests" / "test_counter.py").is_file()
    assert (dest / "tests" / "test_format.py").is_file()
    assert (dest / "tests" / "fixture_checks.py").is_file()

    # 1. Initial run: MUST FAIL (adds 2, no -1 assertion)
    ret, out, _err = _run_cmd([sys.executable, "-I", "-B", "tests/fixture_checks.py"], dest)
    assert ret == 1
    report = _parse_report(out)
    assert report["fixture_version"] == FIXTURE_VERSION
    assert report["case_key"] == "counter-service"
    assert report["command_name"] == "unit"
    assertions = report["assertions"]
    assert isinstance(assertions, dict)
    assert assertions["increment_adds_one"] is False
    assert assertions["boundary_negative_one"] is False
    assert assertions["format_preserved"] is True

    # 2. Injected wrong patch: change + 2 to + 0. MUST FAIL.
    counter_py = dest / "src" / "counter.py"
    content = counter_py.read_text(encoding="utf-8")
    counter_py.write_text(content.replace("return value + 2", "return value + 0"), encoding="utf-8")

    ret, out, _err = _run_cmd([sys.executable, "-I", "-B", "tests/fixture_checks.py"], dest)
    assert ret == 1
    report = _parse_report(out)
    assertions = report["assertions"]
    assert isinstance(assertions, dict)
    assert assertions["increment_adds_one"] is False

    # 3. Correct repair: change + 0 to + 1 and add -1 boundary assertion to test_counter.py. MUST PASS.
    counter_py.write_text(content.replace("return value + 2", "return value + 1"), encoding="utf-8")
    test_counter_py = dest / "tests" / "test_counter.py"
    test_content = test_counter_py.read_text(encoding="utf-8")
    test_counter_py.write_text(
        test_content + "\n\ndef test_boundary() -> None:\n    assert increment(-1) == 0\n",
        encoding="utf-8",
    )

    ret, out, _err = _run_cmd([sys.executable, "-I", "-B", "tests/fixture_checks.py"], dest)
    assert ret == 0
    report = _parse_report(out)
    assertions = report["assertions"]
    assert isinstance(assertions, dict)
    assert assertions["increment_adds_one"] is True
    assert assertions["boundary_negative_one"] is True
    assert assertions["format_preserved"] is True
    tests = report["tests"]
    assert isinstance(tests, dict)
    assert tests["tests/test_counter.py"] is True
    assert tests["tests/test_format.py"] is True


def test_split_catalog_isolation_and_copies(tmp_path: Path) -> None:
    dest = tmp_path / "split-catalog"
    fixture = build_split_catalog_fixture(dest)
    assert fixture.fixture_name == "split-catalog"
    assert (dest / "alpha" / "value.txt").read_text(encoding="utf-8").strip() == "old"
    assert (dest / "beta" / "value.txt").read_text(encoding="utf-8").strip() == "old"

    # Initial state: isolated and valid
    ret, out, _err = _run_cmd([sys.executable, "-I", "-B", "tests/fixture_checks.py"], dest)
    assert ret == 0
    report = _parse_report(out)
    assertions = report["assertions"]
    assert isinstance(assertions, dict)
    assert assertions["catalog_halves_isolated"] is True
    assert assertions["alpha_valid"] is True
    assert assertions["beta_valid"] is True

    # Alpha task: update alpha only
    (dest / "alpha" / "value.txt").write_text("alpha-v2\n", encoding="utf-8")
    ret, out, _err = _run_cmd([sys.executable, "-I", "-B", "tests/fixture_checks.py"], dest)
    assert ret == 0
    report = _parse_report(out)
    assertions = report["assertions"]
    assert isinstance(assertions, dict)
    assert assertions["catalog_halves_isolated"] is True
    assert (dest / "beta" / "value.txt").read_text(encoding="utf-8").strip() == "old"

    # Beta task: update beta
    (dest / "beta" / "value.txt").write_text("beta-v2\n", encoding="utf-8")
    ret, out, _err = _run_cmd([sys.executable, "-I", "-B", "tests/fixture_checks.py"], dest)
    assert ret == 0
    report = _parse_report(out)
    assertions = report["assertions"]
    assert isinstance(assertions, dict)
    assert assertions["catalog_halves_isolated"] is True

    # Cross-contamination: beta writer writes into alpha directory
    (dest / "alpha" / "value.txt").write_text("beta-v2\n", encoding="utf-8")
    ret, out, _err = _run_cmd([sys.executable, "-I", "-B", "tests/fixture_checks.py"], dest)
    assert ret == 1
    report = _parse_report(out)
    assertions = report["assertions"]
    assert isinstance(assertions, dict)
    assert assertions["catalog_halves_isolated"] is False

    # Foreign file contamination
    (dest / "alpha" / "value.txt").write_text("alpha-v2\n", encoding="utf-8")
    (dest / "alpha" / "rogue.txt").write_text("rogue\n", encoding="utf-8")
    ret, out, _err = _run_cmd([sys.executable, "-I", "-B", "tests/fixture_checks.py"], dest)
    assert ret == 1

    # Test build_catalog_copies for A3 concurrency
    dest_a = tmp_path / "catalog-A"
    dest_b = tmp_path / "catalog-B"
    fix_a, fix_b = build_catalog_copies(dest_a, dest_b)
    assert fix_a.fixture_name == "catalog-A"
    assert fix_b.fixture_name == "catalog-B"
    assert fix_a.manifest.content_digest == fix_b.manifest.content_digest
    assert (dest_a / "alpha" / "value.txt").exists()
    assert (dest_b / "alpha" / "value.txt").exists()


def test_review_gates_boundary_cases_and_typo(tmp_path: Path) -> None:
    dest = tmp_path / "review-gates"
    fixture = build_review_gates_fixture(dest)
    assert fixture.fixture_name == "review-gates"

    # 1. Initial state: range buggy (omits stop) and typo present. MUST FAIL.
    ret, out, _err = _run_cmd([sys.executable, "-I", "-B", "tests/fixture_checks.py"], dest)
    assert ret == 1
    report = _parse_report(out)
    assertions = report["assertions"]
    assert isinstance(assertions, dict)
    assert assertions["inclusive_contract"] is False
    assert assertions["typo_fixed"] is False

    # 2. A5 typo task: fix ONLY docs/result.md
    result_md = dest / "docs" / "result.md"
    result_md.write_text(
        result_md.read_text(encoding="utf-8").replace("teh result", "the result"), encoding="utf-8"
    )

    ret, out, _err = _run_cmd([sys.executable, "-I", "-B", "tests/fixture_checks.py"], dest)
    assert ret == 0
    report = _parse_report(out)
    assertions = report["assertions"]
    assert isinstance(assertions, dict)
    assert assertions["typo_fixed"] is True
    tests = report["tests"]
    assert isinstance(tests, dict)
    assert tests["docs/result.md"] is True

    # 3. A4 injected fault: reset typo back, fix range in src/range.py,
    # add lower/upper/reversed but OMIT equal-bound
    result_md.write_text(
        result_md.read_text(encoding="utf-8").replace("the result", "teh result"), encoding="utf-8"
    )

    range_py = dest / "src" / "range.py"
    range_py.write_text(
        "def inclusive_range(start: int, stop: int, step: int = 1) -> list[int]:\n"
        "    if step == 0:\n"
        "        raise ValueError('step must not be zero')\n"
        "    if (step > 0 and start > stop) or (step < 0 and start < stop):\n"
        "        return []\n"
        "    res = []\n"
        "    cur = start\n"
        "    if step > 0:\n"
        "        while cur <= stop:\n"
        "            res.append(cur)\n"
        "            cur += step\n"
        "    else:\n"
        "        while cur >= stop:\n"
        "            res.append(cur)\n"
        "            cur += step\n"
        "    return res\n",
        encoding="utf-8",
    )

    test_range_py = dest / "tests" / "test_range.py"
    # Covers lower (1), upper (5), reversed (-1), but NO equal-bound (e.g. 3, 3)
    test_range_py.write_text(
        "from range import inclusive_range\n\n"
        "def test_cases() -> None:\n"
        "    r = inclusive_range(1, 5)\n"
        "    assert 1 in r\n"
        "    assert 5 in r\n"
        "    rev = inclusive_range(5, 1, -1)\n"
        "    assert rev == [5, 4, 3, 2, 1]\n",
        encoding="utf-8",
    )

    ret, out, _err = _run_cmd([sys.executable, "-I", "-B", "tests/fixture_checks.py"], dest)
    assert ret == 1
    report = _parse_report(out)
    assertions = report["assertions"]
    assert isinstance(assertions, dict)
    assert assertions["inclusive_contract"] is True
    assert assertions["boundary_cases"] is False
    assert assertions["equal_bound_tested"] is False

    # 4. A4 repair: add equal-bound test case
    test_range_py.write_text(
        test_range_py.read_text(encoding="utf-8")
        + "\n    eq = inclusive_range(3, 3)\n    assert eq == [3]\n",
        encoding="utf-8",
    )

    ret, out, _err = _run_cmd([sys.executable, "-I", "-B", "tests/fixture_checks.py"], dest)
    assert ret == 0
    report = _parse_report(out)
    assertions = report["assertions"]
    assert isinstance(assertions, dict)
    assert assertions["inclusive_contract"] is True
    assert assertions["boundary_cases"] is True
    assert assertions["equal_bound_tested"] is True
    tests = report["tests"]
    assert isinstance(tests, dict)
    assert tests["tests/test_range.py"] is True


def test_slow_unit_barrier_protocol(tmp_path: Path) -> None:
    dest = tmp_path / "catalog-barrier"
    _fixture = build_split_catalog_fixture(dest)
    clean_slow_unit_markers(dest)

    # Launch slow-unit check in background
    cmd = [
        sys.executable,
        "-I",
        "-B",
        "tests/fixture_checks.py",
        "--barrier-file",
        ".forge-acceptance/slow-unit.release",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(dest),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Wait for the atomic entered marker
        entered = wait_slow_unit_entered(dest, timeout_seconds=5.0)
        assert entered is True, "Expected slow-unit.entered marker was not found"

        # Verify process is held at barrier
        time.sleep(0.05)
        assert proc.poll() is None, "Process exited prematurely instead of holding at barrier"

        # Release barrier
        release_slow_unit_barrier(dest)

        stdout, _stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0
        report = _parse_report(stdout)
        assert report["command_name"] == "slow-unit"
        assertions = report["assertions"]
        assert isinstance(assertions, dict)
        assert assertions["catalog_halves_isolated"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        clean_slow_unit_markers(dest)


def test_slow_unit_timeout_does_not_hang(tmp_path: Path) -> None:
    dest = tmp_path / "catalog-timeout"
    _fixture = build_split_catalog_fixture(dest)
    clean_slow_unit_markers(dest)

    # Grader source has a 15s deadline for barrier release. Run with nonexistent barrier file.
    cmd = [
        sys.executable,
        "-I",
        "-B",
        "tests/fixture_checks.py",
        "--barrier-file",
        ".forge-acceptance/never-released.release",
    ]
    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=str(dest),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Barrier will time out in ~15s; verify process terminates cleanly with non-zero
    _stdout, stderr = proc.communicate(timeout=25)
    elapsed = time.monotonic() - start
    assert proc.returncode != 0
    assert "TimeoutError" in stderr or "Timed out" in stderr
    assert elapsed >= 14.0, "Barrier exited too quickly"
    clean_slow_unit_markers(dest)


def test_safe_disposable_paths_and_refuse_unsafe(tmp_path: Path) -> None:
    # 1. Refuse existing non-empty directory
    non_empty = tmp_path / "non_empty"
    non_empty.mkdir()
    (non_empty / "file.txt").write_text("data")
    with pytest.raises(UnsafeFixturePathError, match="not empty"):
        build_counter_service_fixture(non_empty)

    # 2. Refuse existing git repository
    existing_git = tmp_path / "existing_git"
    existing_git.mkdir()
    (existing_git / ".git").mkdir()
    with pytest.raises(UnsafeFixturePathError, match="existing .git"):
        build_counter_service_fixture(existing_git)

    # 3. Refuse canonical workspace root
    cwd = Path.cwd()
    with pytest.raises(UnsafeFixturePathError, match="overlaps canonical"):
        build_counter_service_fixture(cwd)

    # 4. Context manager auto cleanup
    temp_path: Path
    with materialize_subscription_fixture("split-catalog") as fix:
        temp_path = fix.path
        assert temp_path.exists()
        assert (temp_path / ".git").is_dir()
    assert not temp_path.exists(), "Temporary fixture was not cleaned up after context exit"

    # 5. Context manager preserve
    with materialize_subscription_fixture("split-catalog") as fix:
        preserved_path = fix.path
        fix.preserve()
    assert preserved_path.exists(), "Preserved temporary fixture was prematurely deleted"
    # Cleanup preserved fixture
    shutil.rmtree(preserved_path, onerror=_remove_readonly)


@pytest.mark.asyncio
async def test_compatibility_with_read_check_evidence(tmp_path: Path) -> None:
    """Verify emitted reports decode properly through read_check_evidence."""
    store = FilesystemArtifactStore(tmp_path / "artifacts")

    # 1. Passing counter-service report
    counter_case = get_counter_service_case()
    counter_report = {
        "report_version": 1,
        "fixture_version": FIXTURE_VERSION,
        "case_key": "counter-service",
        "command_name": "unit",
        "tests": {"tests/test_counter.py": True, "tests/test_format.py": True},
        "assertions": {
            "increment_adds_one": True,
            "boundary_negative_one": True,
            "format_preserved": True,
        },
    }
    line = REPORT_PREFIX + json.dumps(counter_report)
    output = {"stream": "stdout", "truncated": False, "text": line}
    stdout = await store.put_bytes(json.dumps(output).encode(), media_type="application/json")
    receipt = {
        "receipt_version": 1,
        "tool_call_id": "call-counter",
        "stdout_digest": stdout.digest,
        "caller_cancelled": False,
        "request_payload": {"command_name": "unit"},
    }
    saved = await store.put_bytes(json.dumps(receipt).encode(), media_type="application/json")
    result = {
        "status": "succeeded",
        "tool_call_id": "call-counter",
        "artifact_digests": [saved.digest],
        "metadata": {
            "receipt_digest": saved.digest,
            "stdout_digest": stdout.digest,
            "exit_code": 0,
            "timed_out": False,
            "caller_cancelled": False,
        },
    }
    scores = await read_check_evidence(counter_case, store, result, command_name="unit")
    assert scores is not None
    tests, assertions = scores
    assert tests == {"tests/test_counter.py": True, "tests/test_format.py": True}
    assert assertions == {
        "increment_adds_one": True,
        "boundary_negative_one": True,
        "format_preserved": True,
    }

    # 2. Failing report (exit_code != 0) earns no credit
    failing_result = {
        "status": "succeeded",
        "tool_call_id": "call-counter",
        "artifact_digests": [saved.digest],
        "metadata": {
            "receipt_digest": saved.digest,
            "stdout_digest": stdout.digest,
            "exit_code": 1,
            "timed_out": False,
            "caller_cancelled": False,
        },
    }
    assert (
        await read_check_evidence(counter_case, store, failing_result, command_name="unit") is None
    )

    # 3. Passing split-catalog alpha report
    alpha_case = get_split_catalog_alpha_case()
    catalog_report = {
        "report_version": 1,
        "fixture_version": FIXTURE_VERSION,
        "case_key": "split-catalog",
        "command_name": "unit",
        "tests": {"alpha/value.txt": True, "beta/value.txt": True},
        "assertions": {
            "alpha_valid": True,
            "beta_valid": True,
            "catalog_halves_isolated": True,
        },
    }
    line = REPORT_PREFIX + json.dumps(catalog_report)
    output = {"stream": "stdout", "truncated": False, "text": line}
    stdout = await store.put_bytes(json.dumps(output).encode(), media_type="application/json")
    receipt = {
        "receipt_version": 1,
        "tool_call_id": "call-catalog",
        "stdout_digest": stdout.digest,
        "caller_cancelled": False,
        "request_payload": {"command_name": "unit"},
    }
    saved = await store.put_bytes(json.dumps(receipt).encode(), media_type="application/json")
    result = {
        "status": "succeeded",
        "tool_call_id": "call-catalog",
        "artifact_digests": [saved.digest],
        "metadata": {
            "receipt_digest": saved.digest,
            "stdout_digest": stdout.digest,
            "exit_code": 0,
            "timed_out": False,
            "caller_cancelled": False,
        },
    }
    scores = await read_check_evidence(alpha_case, store, result, command_name="unit")
    assert scores is not None
    tests, assertions = scores
    assert tests == {"alpha/value.txt": True, "beta/value.txt": True}
    assert assertions == {
        "alpha_valid": True,
        "beta_valid": True,
        "catalog_halves_isolated": True,
    }

    # 4. Passing review-gates range report
    range_case = get_review_gates_range_case()
    range_report = {
        "report_version": 1,
        "fixture_version": FIXTURE_VERSION,
        "case_key": "review-gates",
        "command_name": "unit",
        "tests": {"tests/test_range.py": True},
        "assertions": {
            "inclusive_contract": True,
            "boundary_cases": True,
            "equal_bound_tested": True,
        },
    }
    line = REPORT_PREFIX + json.dumps(range_report)
    output = {"stream": "stdout", "truncated": False, "text": line}
    stdout = await store.put_bytes(json.dumps(output).encode(), media_type="application/json")
    receipt = {
        "receipt_version": 1,
        "tool_call_id": "call-range",
        "stdout_digest": stdout.digest,
        "caller_cancelled": False,
        "request_payload": {"command_name": "unit"},
    }
    saved = await store.put_bytes(json.dumps(receipt).encode(), media_type="application/json")
    result = {
        "status": "succeeded",
        "tool_call_id": "call-range",
        "artifact_digests": [saved.digest],
        "metadata": {
            "receipt_digest": saved.digest,
            "stdout_digest": stdout.digest,
            "exit_code": 0,
            "timed_out": False,
            "caller_cancelled": False,
        },
    }
    scores = await read_check_evidence(range_case, store, result, command_name="unit")
    assert scores is not None
    tests, assertions = scores
    assert tests == {"tests/test_range.py": True}
    assert assertions == {
        "inclusive_contract": True,
        "boundary_cases": True,
        "equal_bound_tested": True,
    }

    # 5. Passing review-gates typo report
    typo_case = get_review_gates_typo_case()
    typo_report = {
        "report_version": 1,
        "fixture_version": FIXTURE_VERSION,
        "case_key": "review-gates",
        "command_name": "unit",
        "tests": {"docs/result.md": True},
        "assertions": {"typo_fixed": True},
    }
    line = REPORT_PREFIX + json.dumps(typo_report)
    output = {"stream": "stdout", "truncated": False, "text": line}
    stdout = await store.put_bytes(json.dumps(output).encode(), media_type="application/json")
    receipt = {
        "receipt_version": 1,
        "tool_call_id": "call-typo",
        "stdout_digest": stdout.digest,
        "caller_cancelled": False,
        "request_payload": {"command_name": "unit"},
    }
    saved = await store.put_bytes(json.dumps(receipt).encode(), media_type="application/json")
    result = {
        "status": "succeeded",
        "tool_call_id": "call-typo",
        "artifact_digests": [saved.digest],
        "metadata": {
            "receipt_digest": saved.digest,
            "stdout_digest": stdout.digest,
            "exit_code": 0,
            "timed_out": False,
            "caller_cancelled": False,
        },
    }
    scores = await read_check_evidence(typo_case, store, result, command_name="unit")
    assert scores is not None
    tests, assertions = scores
    assert tests == {"docs/result.md": True}
    assert assertions == {"typo_fixed": True}
