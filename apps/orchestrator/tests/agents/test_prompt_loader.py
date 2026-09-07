"""Tests for deterministic versioned role instruction loading and drift detection."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from forge.agents.prompt_loader import (
    LoadedPrompt,
    PromptChanged,
    PromptLoader,
    PromptLoadError,
)
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentBudget,
    AgentRequest,
    PlannerInput,
    PolicySummary,
    UntrustedContent,
    UntrustedSourceKind,
)
from forge.domain.policy import RunnerMode
from forge.domain.review import FindingSeverity
from forge.domain.tool import ToolName

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENTS_DIR = REPO_ROOT / "agents"


def _make_sample_request(
    *,
    role: AgentRole = AgentRole.PLANNER,
    version: str = "1",
    instruction: str = "<!-- forge-instruction-version: 1 -->\nValid body",
    digest: str | None = None,
) -> AgentRequest:
    """Build an AgentRequest matching the given instruction metadata."""
    calculated_digest = digest or hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    untrusted_task = UntrustedContent.from_text(
        "Sample task",
        source_kind=UntrustedSourceKind.TASK,
        source_reference="sample-ref",
    )
    untrusted_tree = UntrustedContent.from_text(
        "tree:\n- file.txt",
        source_kind=UntrustedSourceKind.REPOSITORY_TREE,
        source_reference="tree-ref",
    )
    context = PlannerInput(
        original_task=untrusted_task,
        base_commit="a" * 40,
        repository_tree=untrusted_tree,
        relevant_instructions=(),
        policy_summary=PolicySummary(
            policy_id=uuid4(),
            policy_version=1,
            runner_mode=RunnerMode.DOCKER,
            trusted_project=False,
            required_checks=(),
            allowed_merge_methods=("squash",),
            publication_blocking_severities=(FindingSeverity.BLOCKER,),
            merge_blocking_severities=(FindingSeverity.BLOCKER,),
        ),
    )
    return AgentRequest(
        execution_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        role=role,
        context=context,
        parent_execution_id=None,
        provider="fake-provider",
        model="fake-model",
        instruction_version=version,
        system_instruction=instruction,
        instruction_digest=calculated_digest,
        allowed_tools=(ToolName.REPOSITORY_LIST_FILES,),
        budget=AgentBudget(),
    )


# ===========================================================================
# Real Repository Prompts Tests
# ===========================================================================


class TestRepositoryPrompts:
    """Validate that repository prompt files load, hash, and parse properly."""

    @pytest.fixture
    def repo_loader(self) -> PromptLoader:
        assert AGENTS_DIR.is_dir(), f"Expected agents directory at {AGENTS_DIR}"
        return PromptLoader(AGENTS_DIR)

    @pytest.mark.parametrize("role", tuple(AgentRole))
    def test_load_all_repository_roles(self, repo_loader: PromptLoader, role: AgentRole) -> None:
        loaded = repo_loader.load(role)
        assert loaded.role is role
        assert loaded.version == ("3" if role is AgentRole.DEVELOPER else "1")
        assert len(loaded.instruction) > 0
        assert len(loaded.instruction.encode("utf-8")) <= 10_000
        assert loaded.digest == hashlib.sha256(loaded.instruction.encode("utf-8")).hexdigest()
        assert not loaded.instruction.startswith("\ufeff")

    def test_verify_unchanged_succeeds_for_matching_repo_prompt(
        self, repo_loader: PromptLoader
    ) -> None:
        loaded = repo_loader.load(AgentRole.PLANNER)
        request = _make_sample_request(
            role=AgentRole.PLANNER,
            version=loaded.version,
            instruction=loaded.instruction,
            digest=loaded.digest,
        )
        verified = repo_loader.verify_unchanged(request)
        assert verified.role is AgentRole.PLANNER
        assert verified.version == loaded.version
        assert verified.digest == loaded.digest


# ===========================================================================
# Drift Detection Tests
# ===========================================================================


class TestPromptDriftDetection:
    """Test that PromptLoader.verify_unchanged rejects any alteration or drift."""

    def test_drift_detected_when_system_instruction_altered(self, tmp_path: Path) -> None:
        planner_dir = tmp_path / "planner"
        planner_dir.mkdir()
        file_path = planner_dir / "instructions.md"
        content = "<!-- forge-instruction-version: 1 -->\nOriginal prompt body"
        file_path.write_text(content, encoding="utf-8")

        loader = PromptLoader(tmp_path)
        loaded = loader.load(AgentRole.PLANNER)

        # Tampered request instruction body
        tampered_instruction = "<!-- forge-instruction-version: 1 -->\nTampered prompt body"
        req = _make_sample_request(
            role=AgentRole.PLANNER,
            version=loaded.version,
            instruction=tampered_instruction,
        )

        with pytest.raises(PromptChanged):
            loader.verify_unchanged(req)

    def test_drift_detected_when_version_altered(self, tmp_path: Path) -> None:
        planner_dir = tmp_path / "planner"
        planner_dir.mkdir()
        file_path = planner_dir / "instructions.md"
        content = "<!-- forge-instruction-version: 1 -->\nOriginal prompt body"
        file_path.write_text(content, encoding="utf-8")

        loader = PromptLoader(tmp_path)
        loaded = loader.load(AgentRole.PLANNER)

        req = _make_sample_request(
            role=AgentRole.PLANNER,
            version="2",  # Mismatched version
            instruction=loaded.instruction,
            digest=loaded.digest,
        )

        with pytest.raises(PromptChanged):
            loader.verify_unchanged(req)

    def test_drift_detected_when_file_modified_after_request_frozen(self, tmp_path: Path) -> None:
        planner_dir = tmp_path / "planner"
        planner_dir.mkdir()
        file_path = planner_dir / "instructions.md"
        content = "<!-- forge-instruction-version: 1 -->\nInitial instruction content"
        file_path.write_text(content, encoding="utf-8")

        loader = PromptLoader(tmp_path)
        loaded = loader.load(AgentRole.PLANNER)

        req = _make_sample_request(
            role=AgentRole.PLANNER,
            version=loaded.version,
            instruction=loaded.instruction,
            digest=loaded.digest,
        )

        # File is subsequently updated on disk
        file_path.write_text(
            "<!-- forge-instruction-version: 2 -->\nUpdated instruction content",
            encoding="utf-8",
        )

        with pytest.raises(PromptChanged):
            loader.verify_unchanged(req)

    def test_drift_detected_when_file_deleted_after_request_frozen(self, tmp_path: Path) -> None:
        planner_dir = tmp_path / "planner"
        planner_dir.mkdir()
        file_path = planner_dir / "instructions.md"
        content = "<!-- forge-instruction-version: 1 -->\nInitial instruction content"
        file_path.write_text(content, encoding="utf-8")

        loader = PromptLoader(tmp_path)
        loaded = loader.load(AgentRole.PLANNER)

        req = _make_sample_request(
            role=AgentRole.PLANNER,
            version=loaded.version,
            instruction=loaded.instruction,
            digest=loaded.digest,
        )

        file_path.unlink()

        with pytest.raises(PromptChanged):
            loader.verify_unchanged(req)


# ===========================================================================
# Safe Loading & Security Constraints
# ===========================================================================


class TestPromptLoaderSecurityAndErrors:
    """Test safe loading, bounds checking, and input validation."""

    def test_constructor_rejects_non_path(self) -> None:
        with pytest.raises(TypeError, match="instruction root must be a Path"):
            PromptLoader("agents")  # type: ignore[arg-type]

    def test_constructor_rejects_nonexistent_directory(self, tmp_path: Path) -> None:
        missing_dir = tmp_path / "does_not_exist"
        with pytest.raises(PromptLoadError):
            PromptLoader(missing_dir)

    def test_constructor_rejects_file_path(self, tmp_path: Path) -> None:
        file_target = tmp_path / "file.txt"
        file_target.write_text("not a dir", encoding="utf-8")
        with pytest.raises(PromptLoadError):
            PromptLoader(file_target)

    def test_load_rejects_non_role(self, tmp_path: Path) -> None:
        loader = PromptLoader(tmp_path)
        with pytest.raises(TypeError, match="prompt role must be an AgentRole"):
            loader.load("planner")  # type: ignore[arg-type]

    def test_verify_unchanged_rejects_non_request(self, tmp_path: Path) -> None:
        loader = PromptLoader(tmp_path)
        with pytest.raises(TypeError, match="prompt verification requires an AgentRequest"):
            loader.verify_unchanged({"not": "a request"})  # type: ignore[arg-type]

    def test_load_rejects_missing_role_file(self, tmp_path: Path) -> None:
        # Directory exists, but planner/instructions.md is absent
        loader = PromptLoader(tmp_path)
        with pytest.raises(PromptLoadError):
            loader.load(AgentRole.PLANNER)

    def test_load_rejects_utf8_bom(self, tmp_path: Path) -> None:
        planner_dir = tmp_path / "planner"
        planner_dir.mkdir()
        file_path = planner_dir / "instructions.md"
        content = b"\xef\xbb\xbf<!-- forge-instruction-version: 1 -->\nWith BOM"
        file_path.write_bytes(content)

        loader = PromptLoader(tmp_path)
        with pytest.raises(PromptLoadError):
            loader.load(AgentRole.PLANNER)

    def test_load_rejects_oversized_file(self, tmp_path: Path) -> None:
        planner_dir = tmp_path / "planner"
        planner_dir.mkdir()
        file_path = planner_dir / "instructions.md"
        header = "<!-- forge-instruction-version: 1 -->\n"
        oversized = header + ("x" * 10_001)
        file_path.write_text(oversized, encoding="utf-8")

        loader = PromptLoader(tmp_path)
        with pytest.raises(PromptLoadError):
            loader.load(AgentRole.PLANNER)

    def test_load_rejects_non_utf8_encoding(self, tmp_path: Path) -> None:
        planner_dir = tmp_path / "planner"
        planner_dir.mkdir()
        file_path = planner_dir / "instructions.md"
        file_path.write_bytes(b"<!-- forge-instruction-version: 1 -->\n\xff\xfe\xfd")

        loader = PromptLoader(tmp_path)
        with pytest.raises(PromptLoadError):
            loader.load(AgentRole.PLANNER)

    def test_load_rejects_missing_version_header(self, tmp_path: Path) -> None:
        planner_dir = tmp_path / "planner"
        planner_dir.mkdir()
        file_path = planner_dir / "instructions.md"
        file_path.write_text("# Forge Planner\nNo header", encoding="utf-8")

        loader = PromptLoader(tmp_path)
        with pytest.raises(PromptLoadError):
            loader.load(AgentRole.PLANNER)

    def test_load_rejects_malformed_version_header(self, tmp_path: Path) -> None:
        planner_dir = tmp_path / "planner"
        planner_dir.mkdir()
        file_path = planner_dir / "instructions.md"
        file_path.write_text("<!-- version: 1 -->\nBody", encoding="utf-8")

        loader = PromptLoader(tmp_path)
        with pytest.raises(PromptLoadError):
            loader.load(AgentRole.PLANNER)

    def test_load_rejects_duplicate_version_headers(self, tmp_path: Path) -> None:
        planner_dir = tmp_path / "planner"
        planner_dir.mkdir()
        file_path = planner_dir / "instructions.md"
        content = (
            "<!-- forge-instruction-version: 1 -->\nBody\n<!-- forge-instruction-version: 2 -->\n"
        )
        file_path.write_text(content, encoding="utf-8")

        loader = PromptLoader(tmp_path)
        with pytest.raises(PromptLoadError):
            loader.load(AgentRole.PLANNER)

    def test_load_rejects_blank_body_after_header(self, tmp_path: Path) -> None:
        planner_dir = tmp_path / "planner"
        planner_dir.mkdir()
        file_path = planner_dir / "instructions.md"
        file_path.write_text("<!-- forge-instruction-version: 1 -->\n   \n   ", encoding="utf-8")

        loader = PromptLoader(tmp_path)
        with pytest.raises(PromptLoadError):
            loader.load(AgentRole.PLANNER)


# ===========================================================================
# LoadedPrompt Dataclass Invariant Tests
# ===========================================================================


class TestLoadedPromptInvariants:
    """Test validation invariants on the LoadedPrompt data transfer object."""

    def test_valid_loaded_prompt(self) -> None:
        instruction = "<!-- forge-instruction-version: 1 -->\nInstruction body"
        digest = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
        loaded = LoadedPrompt(
            role=AgentRole.PLANNER,
            version="1",
            instruction=instruction,
            digest=digest,
        )
        assert loaded.role == AgentRole.PLANNER
        assert loaded.version == "1"
        assert loaded.digest == digest

    def test_rejects_invalid_role_type(self) -> None:
        instruction = "<!-- forge-instruction-version: 1 -->\nBody"
        digest = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
        with pytest.raises(TypeError, match="loaded prompt role must be an AgentRole"):
            LoadedPrompt(
                role="planner",  # type: ignore[arg-type]
                version="1",
                instruction=instruction,
                digest=digest,
            )

    def test_rejects_version_mismatch(self) -> None:
        instruction = "<!-- forge-instruction-version: 1 -->\nBody"
        digest = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
        with pytest.raises(ValueError, match="loaded prompt version does not match instruction"):
            LoadedPrompt(
                role=AgentRole.PLANNER,
                version="2",
                instruction=instruction,
                digest=digest,
            )

    def test_rejects_digest_mismatch(self) -> None:
        instruction = "<!-- forge-instruction-version: 1 -->\nBody"
        with pytest.raises(ValueError, match="loaded prompt digest does not match instruction"):
            LoadedPrompt(
                role=AgentRole.PLANNER,
                version="1",
                instruction=instruction,
                digest="0" * 64,
            )
