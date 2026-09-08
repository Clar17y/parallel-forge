"""Tests for versioned evaluation fixture loading and safe materialization."""

from pathlib import Path

import pytest
from forge.domain.actor import AgentRole
from forge.domain.agent import ReviewOutput
from forge.domain.evaluation import score_plan, score_review
from forge.domain.plan import PlanOutput
from forge.evaluations import (
    CredentialDetectedError,
    EvaluationCaseContract,
    FixtureNotFoundError,
    InvalidCaseContractError,
    UnsafeFixturePathError,
    load_evaluation_case,
    load_evaluation_cases,
    load_expected_output,
    materialize_fixture,
)

FIXTURES_ROOT = Path(__file__).parent / "fixtures"
EXPECTED_ROOT = Path(__file__).parent / "expected"


def test_planner_actual_fixture_loads_materializes_and_scores_cleanly() -> None:
    planner_case_dir = FIXTURES_ROOT / "planner" / "basic-change"
    case = load_evaluation_case(planner_case_dir)

    assert isinstance(case, EvaluationCaseContract)
    assert case.role == AgentRole.PLANNER
    assert case.fixture_version == "eval-fixture-v1"
    assert case.case_key == "planner/basic-change"
    assert case.expected_components == ("apps/web",)
    assert case.expected_checks == ("test", "typecheck")
    assert case.expected_risks == ("authorization",)

    # Deterministic materialization
    with materialize_fixture(case) as mat1:
        assert mat1.path.is_dir()
        assert (mat1.path / ".git").is_dir()
        assert (mat1.path / "apps" / "web" / "src" / "index.ts").is_file()
        first_commit = mat1.base_commit
        first_identity = mat1.fixture_identity

    # Second materialization produces exact same commit and identity
    with materialize_fixture(case) as mat2:
        assert mat2.base_commit == first_commit
        assert mat2.fixture_identity == first_identity

    # Expected output matches and scores 1.0
    expected_data = load_expected_output(EXPECTED_ROOT / "planner-basic-change.json")
    plan = PlanOutput.model_validate(expected_data)
    scores = score_plan(
        expected_components=set(case.expected_components),
        expected_checks=set(case.expected_checks),
        expected_risks=set(case.expected_risks),
        expected_dependencies=set(case.expected_dependencies),
        actual=plan,
        denied_tool_calls=[],
    )
    assert scores.component_recall == 1.0
    assert scores.check_recall == 1.0
    assert scores.risk_recall == 1.0
    assert scores.policy_compliance == 1.0
    assert scores.schema_validity == 1.0


def test_reviewer_actual_fixture_loads_materializes_and_scores_cleanly() -> None:
    reviewer_case_dir = FIXTURES_ROOT / "reviewer" / "missing-authorization"
    case = load_evaluation_case(reviewer_case_dir)

    assert isinstance(case, EvaluationCaseContract)
    assert case.role == AgentRole.REVIEWER
    assert case.fixture_version == "eval-fixture-v1"
    assert "missing-authorization" in case.expected_defects
    defect = case.expected_defects["missing-authorization"]
    assert defect.path == "api.py"
    assert defect.start_line == 7
    assert defect.evidence_anchor == "delete_item()"

    with materialize_fixture(case) as mat:
        assert mat.path.is_dir()
        api_file = mat.path / "api.py"
        assert api_file.is_file()
        lines = api_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) >= 7
        assert "delete_item" in lines[6]

    expected_data = load_expected_output(EXPECTED_ROOT / "reviewer-missing-authorization.json")
    review = ReviewOutput.model_validate(expected_data)
    scores = score_review(
        seeded_defects=case.expected_defects,
        findings=review.findings,
        denied_tool_calls=[],
    )
    assert scores.defect_recall == 1.0
    assert scores.blocker_recall == 1.0
    assert scores.false_positive_count == 0
    assert scores.evidence_quality == 1.0
    assert scores.policy_compliance == 1.0
    assert scores.schema_validity == 1.0


def test_discovery_loads_all_fixtures() -> None:
    cases = load_evaluation_cases(FIXTURES_ROOT)
    assert "planner/basic-change" in cases
    assert "reviewer/missing-authorization" in cases


def test_template_directory_remains_unmodified(tmp_path: Path) -> None:
    case_dir = tmp_path / "sample"
    repo_template = case_dir / "repository"
    repo_template.mkdir(parents=True)
    f1 = repo_template / "file.txt"
    f1.write_text("content\n", encoding="utf-8")
    before_mtime = f1.stat().st_mtime
    before_files = sorted(p.name for p in repo_template.iterdir())

    task_json = case_dir / "task.json"
    task_json.write_text(
        """{
        "fixture_version": "v1",
        "case_key": "sample",
        "task": "do something",
        "role": "planner"
    }""",
        encoding="utf-8",
    )

    case = load_evaluation_case(case_dir)
    with materialize_fixture(case) as mat:
        assert (mat.path / "file.txt").exists()

    after_files = sorted(p.name for p in repo_template.iterdir())
    assert before_files == after_files
    assert f1.stat().st_mtime == before_mtime


def test_different_template_content_changes_identity_and_commit(tmp_path: Path) -> None:
    def create_case(suffix: str, content: str) -> EvaluationCaseContract:
        case_dir = tmp_path / f"case_{suffix}"
        repo = case_dir / "repository"
        repo.mkdir(parents=True)
        (repo / "code.py").write_text(content, encoding="utf-8")
        (case_dir / "task.json").write_text(
            f'{{"fixture_version": "v1", "case_key": "case_{suffix}", "task": "task", "role": "planner"}}',
            encoding="utf-8",
        )
        return load_evaluation_case(case_dir)

    case_a = create_case("a", "version = 1\n")
    case_b = create_case("b", "version = 2\n")

    with materialize_fixture(case_a) as mat_a, materialize_fixture(case_b) as mat_b:
        assert mat_a.fixture_identity != mat_b.fixture_identity
        assert mat_a.base_commit != mat_b.base_commit


def test_cleanup_on_exception_inside_context_manager(tmp_path: Path) -> None:
    case_dir = tmp_path / "fail_case"
    repo = case_dir / "repository"
    repo.mkdir(parents=True)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    (case_dir / "task.json").write_text(
        '{"fixture_version": "v1", "case_key": "fail_case", "task": "task", "role": "planner"}',
        encoding="utf-8",
    )
    case = load_evaluation_case(case_dir)

    mat_path: Path | None = None
    with (
        pytest.raises(RuntimeError, match="deliberate failure"),
        materialize_fixture(case) as mat,
    ):
        mat_path = mat.path
        assert mat_path.exists()
        raise RuntimeError("deliberate failure")

    assert mat_path is not None
    assert not mat_path.exists()


def test_reject_git_directory_inside_template(tmp_path: Path) -> None:
    case_dir = tmp_path / "git_case"
    repo = case_dir / "repository"
    (repo / ".git").mkdir(parents=True)
    (repo / "file.txt").write_text("code\n", encoding="utf-8")
    (case_dir / "task.json").write_text(
        '{"fixture_version": "v1", "case_key": "git_case", "task": "task", "role": "planner"}',
        encoding="utf-8",
    )
    case = load_evaluation_case(case_dir)

    with pytest.raises(UnsafeFixturePathError, match=r"\.git"), materialize_fixture(case):
        pass


def test_reject_credentials_in_case_contract(tmp_path: Path) -> None:
    case_dir = tmp_path / "cred_case"
    case_dir.mkdir(parents=True)
    (case_dir / "task.json").write_text(
        '{"fixture_version": "v1", "case_key": "c", "task": "use token ghp_' + "a" * 36 + '", "role": "planner"}',
        encoding="utf-8",
    )
    with pytest.raises(CredentialDetectedError, match="GitHub personal access token"):
        load_evaluation_case(case_dir)


def test_reject_credentials_in_repository_template(tmp_path: Path) -> None:
    case_dir = tmp_path / "cred_repo"
    repo = case_dir / "repository"
    repo.mkdir(parents=True)
    (repo / "secrets.env").write_text("API_KEY=sk-" + "x" * 30 + "\n", encoding="utf-8")
    (case_dir / "task.json").write_text(
        '{"fixture_version": "v1", "case_key": "cred_repo", "task": "clean", "role": "planner"}',
        encoding="utf-8",
    )
    case = load_evaluation_case(case_dir)
    with pytest.raises(CredentialDetectedError, match="API secret key"), materialize_fixture(case):
        pass


def test_reject_invalid_role(tmp_path: Path) -> None:
    case_dir = tmp_path / "bad_role"
    case_dir.mkdir(parents=True)
    (case_dir / "task.json").write_text(
        '{"fixture_version": "v1", "case_key": "bad", "task": "clean", "role": "admin"}',
        encoding="utf-8",
    )
    with pytest.raises(InvalidCaseContractError, match="invalid agent role"):
        load_evaluation_case(case_dir)


def test_reject_conflicting_role_shapes(tmp_path: Path) -> None:
    case_dir = tmp_path / "conflicting"
    case_dir.mkdir(parents=True)
    (case_dir / "task.json").write_text(
        """{
        "fixture_version": "v1",
        "case_key": "conflicting",
        "task": "clean",
        "role": "planner",
        "expected_defects": {
            "d1": {"severity": "blocker", "path": "a.py", "start_line": 1, "evidence_anchor": "a"}
        }
    }""",
        encoding="utf-8",
    )
    with pytest.raises(InvalidCaseContractError, match="cannot declare expected_defects"):
        load_evaluation_case(case_dir)


def test_reject_missing_case_and_expected_files(tmp_path: Path) -> None:
    with pytest.raises(FixtureNotFoundError):
        load_evaluation_case(tmp_path / "nonexistent")

    with pytest.raises(FixtureNotFoundError):
        load_expected_output(tmp_path / "nonexistent.json")


def test_reject_unbounded_file_size(tmp_path: Path) -> None:
    case_dir = tmp_path / "huge_file"
    repo = case_dir / "repository"
    repo.mkdir(parents=True)
    # Write a file larger than 10MB
    with open(repo / "huge.bin", "wb") as f:
        f.seek(11 * 1024 * 1024)
        f.write(b"\0")
    (case_dir / "task.json").write_text(
        '{"fixture_version": "v1", "case_key": "huge_file", "task": "task", "role": "planner"}',
        encoding="utf-8",
    )
    case = load_evaluation_case(case_dir)
    with pytest.raises(UnsafeFixturePathError, match="size limit"), materialize_fixture(case):
        pass


def test_expected_defects_mapping_is_immutable(tmp_path: Path) -> None:
    case_dir = tmp_path / "reviewer_case"
    case_dir.mkdir(parents=True)
    (case_dir / "task.json").write_text(
        """{
        "fixture_version": "v1",
        "case_key": "reviewer_case",
        "task": "review",
        "role": "reviewer",
        "expected_defects": {
            "d1": {"severity": "blocker", "path": "app.py", "start_line": 1, "evidence_anchor": "bad()"}
        }
    }""",
        encoding="utf-8",
    )
    case = load_evaluation_case(case_dir)
    with pytest.raises(TypeError):
        case.expected_defects["d2"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        case.seeded_defects["d2"] = None  # type: ignore[index]


def test_reject_coerced_string_booleans_in_missing_test(tmp_path: Path) -> None:
    case_dir = tmp_path / "coerce_bool"
    case_dir.mkdir(parents=True)
    (case_dir / "task.json").write_text(
        """{
        "fixture_version": "v1",
        "case_key": "coerce_bool",
        "task": "review",
        "role": "reviewer",
        "expected_defects": {
            "d1": {
                "severity": "blocker",
                "path": "app.py",
                "start_line": 1,
                "evidence_anchor": "bad()",
                "missing_test": "false"
            }
        }
    }""",
        encoding="utf-8",
    )
    with pytest.raises(InvalidCaseContractError, match="missing_test must be a boolean"):
        load_evaluation_case(case_dir)


def test_reject_coerced_string_ints_in_budget(tmp_path: Path) -> None:
    case_dir = tmp_path / "coerce_budget"
    case_dir.mkdir(parents=True)
    (case_dir / "task.json").write_text(
        """{
        "fixture_version": "v1",
        "case_key": "coerce_budget",
        "task": "plan",
        "role": "planner",
        "max_cost_minor": "100"
    }""",
        encoding="utf-8",
    )
    with pytest.raises(InvalidCaseContractError, match="max_cost_minor must be a nonnegative integer"):
        load_evaluation_case(case_dir)


def test_reject_boolean_in_budget_and_defect_start_line(tmp_path: Path) -> None:
    case_dir = tmp_path / "bool_in_int"
    case_dir.mkdir(parents=True)
    (case_dir / "task.json").write_text(
        """{
        "fixture_version": "v1",
        "case_key": "bool_in_int",
        "task": "plan",
        "role": "planner",
        "max_cost_minor": true
    }""",
        encoding="utf-8",
    )
    with pytest.raises(InvalidCaseContractError, match="max_cost_minor must be a nonnegative integer"):
        load_evaluation_case(case_dir)

    defect_dir = tmp_path / "bool_start_line"
    defect_dir.mkdir(parents=True)
    (defect_dir / "task.json").write_text(
        """{
        "fixture_version": "v1",
        "case_key": "bool_start_line",
        "task": "review",
        "role": "reviewer",
        "expected_defects": {
            "d1": {
                "severity": "blocker",
                "path": "app.py",
                "start_line": true,
                "evidence_anchor": "bad()"
            }
        }
    }""",
        encoding="utf-8",
    )
    with pytest.raises(InvalidCaseContractError, match="start_line must be a positive integer"):
        load_evaluation_case(defect_dir)


def test_reject_unknown_fields_in_defects(tmp_path: Path) -> None:
    case_dir = tmp_path / "unknown_defect_field"
    case_dir.mkdir(parents=True)
    (case_dir / "task.json").write_text(
        """{
        "fixture_version": "v1",
        "case_key": "unknown_defect_field",
        "task": "review",
        "role": "reviewer",
        "expected_defects": {
            "d1": {
                "severity": "blocker",
                "path": "app.py",
                "start_line": 1,
                "evidence_anchor": "bad()",
                "extra_payload": "disallowed"
            }
        }
    }""",
        encoding="utf-8",
    )
    with pytest.raises(InvalidCaseContractError, match="unknown fields"):
        load_evaluation_case(case_dir)


def test_reject_invalid_named_prohibited_tools(tmp_path: Path) -> None:
    case_dir = tmp_path / "bad_tools"
    case_dir.mkdir(parents=True)
    (case_dir / "task.json").write_text(
        """{
        "fixture_version": "v1",
        "case_key": "bad_tools",
        "task": "plan",
        "role": "planner",
        "prohibited_tools": ["arbitrary.command_exec"]
    }""",
        encoding="utf-8",
    )
    with pytest.raises(InvalidCaseContractError, match="invalid prohibited tool"):
        load_evaluation_case(case_dir)


def test_template_path_escape_rejected_before_resolution(tmp_path: Path) -> None:
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True)
    (outside_dir / "secret.txt").write_text("secret\n", encoding="utf-8")

    case_dir = tmp_path / "case_escape"
    case_dir.mkdir(parents=True)
    (case_dir / "task.json").write_text(
        """{
        "fixture_version": "v1",
        "case_key": "escape",
        "task": "task",
        "role": "planner",
        "repository_template": "../outside"
    }""",
        encoding="utf-8",
    )
    case = load_evaluation_case(case_dir)
    with pytest.raises(UnsafeFixturePathError, match="repository template"), materialize_fixture(case):
        pass


def test_supplied_destination_existing_rejected(tmp_path: Path) -> None:
    case_dir = tmp_path / "dest_case"
    repo = case_dir / "repository"
    repo.mkdir(parents=True)
    (repo / "file.txt").write_text("ok\n", encoding="utf-8")
    (case_dir / "task.json").write_text(
        '{"fixture_version": "v1", "case_key": "dest_case", "task": "task", "role": "planner"}',
        encoding="utf-8",
    )
    case = load_evaluation_case(case_dir)

    existing_dest = tmp_path / "already_exists"
    existing_dest.mkdir(parents=True)
    with pytest.raises(UnsafeFixturePathError, match="destination"), materialize_fixture(case, destination=existing_dest):
        pass


def test_materialize_never_removes_caller_owned_path(tmp_path: Path) -> None:
    case_dir = tmp_path / "caller_owned_case"
    repo = case_dir / "repository"
    repo.mkdir(parents=True)
    (repo / "file.txt").write_text("ok\n", encoding="utf-8")
    (case_dir / "task.json").write_text(
        '{"fixture_version": "v1", "case_key": "caller_owned_case", "task": "task", "role": "planner"}',
        encoding="utf-8",
    )
    case = load_evaluation_case(case_dir)

    caller_dest = tmp_path / "new_caller_dest"
    assert not caller_dest.exists()

    with (
        pytest.raises(RuntimeError, match="caller intentional error"),
        materialize_fixture(case, destination=caller_dest) as mat,
    ):
        assert mat.path == caller_dest
        assert caller_dest.exists()
        raise RuntimeError("caller intentional error")

    # Caller-owned path must NOT be deleted by materialize_fixture
    assert caller_dest.exists()


def test_git_environment_isolated_from_outer_git_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case_dir = tmp_path / "git_env_case"
    repo = case_dir / "repository"
    repo.mkdir(parents=True)
    (repo / "file.txt").write_text("isolated\n", encoding="utf-8")
    (case_dir / "task.json").write_text(
        '{"fixture_version": "v1", "case_key": "git_env_case", "task": "task", "role": "planner"}',
        encoding="utf-8",
    )
    case = load_evaluation_case(case_dir)

    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent" / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmp_path / "nonexistent_templates"))

    with materialize_fixture(case) as mat:
        assert (mat.path / "file.txt").is_file()
        assert len(mat.base_commit) == 40


def test_materialization_uses_snapshot_bytes_not_disk_reread(tmp_path: Path) -> None:
    case_dir = tmp_path / "snapshot_case"
    repo = case_dir / "repository"
    repo.mkdir(parents=True)
    source_file = repo / "code.py"
    source_file.write_text("original_content = 1\n", encoding="utf-8")
    (case_dir / "task.json").write_text(
        '{"fixture_version": "v1", "case_key": "snapshot_case", "task": "task", "role": "planner"}',
        encoding="utf-8",
    )
    case = load_evaluation_case(case_dir)

    # Monkeypatch or simulate modification on disk during validation -> materialization:
    # We call validate_repository_template to snapshot
    from forge.evaluations.materializer import validate_repository_template
    snapshots = validate_repository_template(repo)
    assert len(snapshots) == 1
    assert snapshots[0].rel_path == "code.py"
    assert b"original_content" in snapshots[0].content

    # Now modify source_file on disk to something completely different
    source_file.write_text("tampered_content = 2\n", encoding="utf-8")

    # When materializing with pre-validated snapshots, it writes the snapshotted bytes
    with materialize_fixture(case, snapshots=snapshots) as mat:
        mat_file = mat.path / "code.py"
        assert mat_file.read_text(encoding="utf-8") == "original_content = 1\n"


@pytest.mark.parametrize("relative, size, digest", [
    ("../escape.txt", 4, None), (".git/config", 4, None),
    ("safe.txt", 99, None), ("safe.txt", 4, "0" * 64),
])
def test_supplied_snapshots_cannot_bypass_validation(tmp_path, relative, size, digest):
    import hashlib

    from forge.evaluations.materializer import TemplateSnapshotFile

    case_dir = tmp_path / "case"
    (case_dir / "repository").mkdir(parents=True)
    (case_dir / "task.json").write_text(
        '{"fixture_version":"v1","case_key":"snapshot","task":"task","role":"planner"}',
        encoding="utf-8",
    )
    case = load_evaluation_case(case_dir)
    snapshot = TemplateSnapshotFile(relative, b"safe", size, digest or hashlib.sha256(b"safe").hexdigest())
    with pytest.raises(UnsafeFixturePathError), materialize_fixture(
        case, destination=tmp_path / "destination", snapshots=[snapshot],
    ):
        pass
    assert not (tmp_path / "escape.txt").exists()


def test_fixture_commit_contains_template_files_even_when_ignored(tmp_path):
    import subprocess

    case_dir = tmp_path / "case"
    repository = case_dir / "repository"
    repository.mkdir(parents=True)
    (repository / ".gitignore").write_text("needed.txt\n", encoding="utf-8")
    (repository / "needed.txt").write_text("fixture input\n", encoding="utf-8")
    (case_dir / "task.json").write_text(
        '{"fixture_version":"v1","case_key":"ignored","task":"task","role":"planner"}',
        encoding="utf-8",
    )
    with materialize_fixture(load_evaluation_case(case_dir)) as materialized:
        tracked = subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", materialized.base_commit],
            cwd=materialized.path, text=True, timeout=10,
        ).splitlines()
        assert tracked == [".gitignore", "needed.txt"]
