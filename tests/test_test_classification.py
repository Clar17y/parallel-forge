"""Offline selection follows real fixture prerequisites, not a test's directory."""

import os
import subprocess
import sys
from pathlib import Path


def test_offline_collection_excludes_transitive_database_fixtures_but_keeps_helpers(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    case = tmp_path / "test_prerequisites.py"
    case.write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def database_alias(session_factory): return session_factory\n"
        "def test_database_direct(test_database_url): pass\n"
        "def test_database_transitive(database_alias): pass\n"
        "def test_plain_unit(): pass\n"
        "def test_database_name_helper(database_name_validator): pass\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(repository / "pyproject.toml"),
            "-p",
            "apps.orchestrator.tests.persistence.conftest",
            "--collect-only",
            "-q",
            "-m",
            "not integration and not docker and not live_provider and not live_github",
            str(case),
        ],
        cwd=repository,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::test_plain_unit" in result.stdout
    assert "::test_database_name_helper" in result.stdout
    assert "::test_database_direct" not in result.stdout
    assert "::test_database_transitive" not in result.stdout
    assert "2/4 tests collected (2 deselected)" in result.stdout
