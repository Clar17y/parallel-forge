"""Prior-attempt checkpoint: pure summarization and context bounding."""

from datetime import UTC, datetime
from uuid import uuid4

from forge.application.ports.subscription_recovery import PriorAttemptEvidence
from forge.application.ports.tools import ToolCallRecord
from forge.application.services.subscription_context import (
    bounded_approved_context,
    summarize_prior_attempts,
)
from forge.domain.tool import ToolCallStatus, ToolName

NOW = datetime(2026, 10, 21, tzinfo=UTC)
RUN_ID = uuid4()
TASK_ID = uuid4()


def _call(tool, arguments, *, status=ToolCallStatus.SUCCEEDED, attempt_id=None):
    return ToolCallRecord(
        id=uuid4(),
        run_id=RUN_ID,
        agent_execution_id=None,
        tool_name=tool,
        normalized_arguments=arguments,
        authorized=True,
        status=status,
        started_at=NOW,
        completed_at=NOW,
        subscription_task_id=TASK_ID,
        subscription_attempt_id=attempt_id or uuid4(),
        subscription_purpose="worker",
    )


def _attempt(number, calls, *, disposition="failed", failure="protocol", contract="current"):
    return PriorAttemptEvidence(
        attempt_number=number,
        disposition=disposition,
        failure=failure,
        tool_calls=tuple(calls),
        contract=contract,
    )


def test_checkpoint_labels_pre_plan_attempts():
    checkpoint = summarize_prior_attempts(
        [
            _attempt(1, [], disposition="plan_approval", failure=None, contract="pre_plan"),
            _attempt(2, [], disposition="failed"),
        ]
    )
    assert checkpoint is not None
    assert [entry["contract"] for entry in checkpoint["attempts"]] == ["pre_plan", "current"]


def test_checkpoint_truncation_drops_oldest_pre_plan_attempt_first():
    # Deterministic degradation: newest attempts are kept, so a lone pre-plan
    # attempt (the oldest) is dropped before any current-contract attempt.
    attempts = [_attempt(1, [], contract="pre_plan")] + [
        _attempt(number, []) for number in range(2, 10)
    ]
    checkpoint = summarize_prior_attempts(attempts)
    assert checkpoint is not None
    assert checkpoint["truncated"] is True
    assert [entry["attempt_number"] for entry in checkpoint["attempts"]] == list(range(2, 10))
    assert all(entry["contract"] == "current" for entry in checkpoint["attempts"])


def test_checkpoint_summarizes_only_persisted_tool_records():
    attempt_id = uuid4()
    calls = [
        _call(ToolName.REPOSITORY_READ_FILE, {"path": "src/a.py"}, attempt_id=attempt_id),
        _call(ToolName.REPOSITORY_READ_FILE, {"path": "src/a.py"}, attempt_id=attempt_id),
        _call(ToolName.REPOSITORY_READ_FILE, {"path": "src/b.py"}, attempt_id=attempt_id),
        _call(
            ToolName.REPOSITORY_SEARCH,
            {"literal": "needle", "path": "src"},
            attempt_id=attempt_id,
        ),
        _call(
            ToolName.REPOSITORY_SEARCH_SEMANTIC,
            {"query": "find thing"},
            attempt_id=attempt_id,
        ),
        _call(
            ToolName.REPOSITORY_WRITE_FILE,
            {"path": "src/a.py", "content": "SECRET-CONTENT-MUST-NOT-APPEAR"},
            attempt_id=attempt_id,
        ),
        _call(
            ToolName.REPOSITORY_DELETE_FILE,
            {"path": "src/old.py", "expected_digest": "x"},
            attempt_id=attempt_id,
        ),
        _call(
            ToolName.REPOSITORY_RENAME_FILE,
            {"source": "src/c.py", "destination": "src/d.py", "expected_digest": "x"},
            attempt_id=attempt_id,
        ),
        _call(ToolName.GIT_COMMIT, {"message": "wip"}, attempt_id=attempt_id),
        _call(
            ToolName.BUILD_RUN_NAMED_CHECK,
            {"command_name": "unit"},
            status=ToolCallStatus.FAILED,
            attempt_id=attempt_id,
        ),
        _call(
            ToolName.BUILD_RUN_NAMED_CHECK,
            {"command_name": "unit"},
            attempt_id=attempt_id,
        ),
        _call(
            ToolName.REPOSITORY_READ_FILE,
            {"path": "src/failed.py"},
            status=ToolCallStatus.FAILED,
            attempt_id=attempt_id,
        ),
    ]
    checkpoint = summarize_prior_attempts([_attempt(1, calls)])
    assert checkpoint is not None
    assert checkpoint["historical"] is True
    (entry,) = checkpoint["attempts"]
    assert entry["attempt_number"] == 1
    assert entry["disposition"] == "failed"
    assert entry["failure"] == "protocol"
    assert entry["files_read"] == ["src/a.py", "src/b.py"]
    assert entry["searches"] == [
        {"literal": "needle", "path": "src"},
        {"query": "find thing"},
    ]
    assert entry["files_written"] == ["src/a.py"]
    assert entry["files_deleted"] == ["src/old.py"]
    assert entry["files_renamed"] == [{"source": "src/c.py", "destination": "src/d.py"}]
    assert entry["git_commits"] == 1
    assert entry["named_checks"] == [
        {"name": "unit", "passed": False, "status": "failed"},
        {"name": "unit", "passed": True, "status": "succeeded"},
    ]
    assert "SECRET-CONTENT-MUST-NOT-APPEAR" not in repr(checkpoint)
    assert "content" not in repr(checkpoint)


def test_checkpoint_is_none_without_prior_attempts():
    assert summarize_prior_attempts([]) is None


def test_oversized_checkpoint_degrades_before_displacing_base_context():
    calls = [
        _call(
            ToolName.REPOSITORY_READ_FILE,
            {"path": f"src/{'d' * 100}/file-{index}.py"},
        )
        for index in range(400)
    ]
    checkpoint = summarize_prior_attempts(
        [_attempt(1, calls), _attempt(2, calls, disposition="failed", failure="deadline")]
    )
    context = {"task": {"text": "x" * 1_045_000}, "policy": {"id": "p"}}
    bounded = bounded_approved_context(context, None, checkpoint=checkpoint)
    # Base task content survives; checkpoint is degraded or dropped, never the reverse.
    assert bounded["task"]["text"] == "x" * 1_045_000
    kept = bounded.get("prior_attempt_checkpoint")
    if kept is not None:
        assert kept["truncated"] is True
        assert [a["attempt_number"] for a in kept["attempts"]] == [2]
