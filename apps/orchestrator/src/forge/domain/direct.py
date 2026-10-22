"""Direct autonomous runs: states, result contract, prompt and CLI argv."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

SUPPORTED_DIRECT_CLIENTS = ("muse_code", "antigravity_cli", "codex_app_server", "claude_code")
_MAX_TASK_BYTES = 64 * 1024

RESULT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "needs_decision", "blocked"]},
        "summary": {"type": "string"},
        "question": {"type": ["string", "null"]},
        "options": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "question", "options"],
    "additionalProperties": False,
}


class DirectRunState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NEEDS_DECISION = "NEEDS_DECISION"


TERMINAL_RUN_STATES = frozenset(
    {DirectRunState.DONE, DirectRunState.FAILED, DirectRunState.CANCELLED,
     DirectRunState.NEEDS_DECISION}
)


class DirectSessionState(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    KILLED = "KILLED"
    TIMED_OUT = "TIMED_OUT"


class DirectRunNotFound(LookupError):
    """The requested direct run does not exist."""


class DirectRunConflict(RuntimeError):
    """The direct run cannot perform the requested transition."""


@dataclass(frozen=True, slots=True)
class DirectRunRecord:
    id: UUID
    project_id: UUID
    task_text: str
    client: str
    model: str
    effort: str | None = None
    state: str = "QUEUED"
    branch: str | None = None
    worktree_path: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    summary: str | None = None
    question: str | None = None
    options: tuple[str, ...] = ()
    failure_reason: str | None = None
    result_degraded: bool = False
    cancel_requested: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DirectSessionRecord:
    id: UUID
    run_id: UUID
    state: str = "RUNNING"
    client: str = ""
    model: str = ""
    effort: str | None = None
    pid: int | None = None
    transcript_path: str | None = None
    last_output_at: datetime | None = None
    output_bytes: int = 0
    exit_code: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DirectResult:
    status: str
    summary: str
    question: str | None = None
    options: tuple[str, ...] = ()
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class DirectLaunch:
    argv: tuple[str, ...]
    stdin_data: bytes | None = None
    final_file: Path | None = None


def parse_direct_result(text: str) -> DirectResult:
    """Parse the structured final result, degrading to a bounded tail summary."""

    candidate = _extract_result(text)
    if candidate is None:
        tail = text.strip()[-2000:]
        return DirectResult(status="done", summary=tail or "session completed", degraded=True)
    status = candidate.get("status")
    if status not in {"done", "needs_decision", "blocked"}:
        tail = text.strip()[-2000:]
        return DirectResult(status="done", summary=tail or "session completed", degraded=True)
    summary = candidate.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = "session completed"
    question = candidate.get("question")
    if not isinstance(question, str):
        question = None
    raw_options = candidate.get("options")
    options = (
        tuple(item for item in raw_options if isinstance(item, str))[:16]
        if isinstance(raw_options, list)
        else ()
    )
    return DirectResult(
        status=status, summary=summary[:4000], question=question, options=options
    )


def _extract_result(text: str) -> dict[str, object] | None:
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    for raw in reversed(fenced):
        parsed = _loads_object(raw)
        if parsed is not None:
            return parsed
    for line in reversed(text.splitlines()):
        parsed = _loads_object(line.strip())
        if parsed is not None and "status" in parsed:
            return parsed
    # Muse JSONL / free text: try the last balanced-looking object substring.
    start = text.rfind('{"status"')
    if start >= 0:
        parsed = _loads_object(text[start : text.find("\n", start) if "\n" in text[start:] else len(text)])
        if parsed is not None:
            return parsed
    return None


def _loads_object(raw: str) -> dict[str, object] | None:
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def build_direct_prompt(task: str, *, has_agents_md: bool) -> str:
    if len(task.encode("utf-8")) > _MAX_TASK_BYTES:
        raise ValueError("direct task text is too large")
    agents = (
        "Read and follow the repository's AGENTS.md before starting.\n" if has_agents_md else ""
    )
    return (
        "You are an autonomous coding agent working in a managed git worktree.\n"
        "Work only inside this worktree; do not modify anything outside it. "
        "Run the relevant tests yourself.\n"
        f"{agents}"
        "\nTask:\n"
        f"{task}\n"
        "\nWhen finished, end your reply with exactly one JSON object in a fenced "
        "json block (and, for Muse, as your structured output):\n"
        '{"status": "done"|"needs_decision"|"blocked", "summary": "...", '
        '"question": null or "...", "options": ["..."]}\n'
        'Use "needs_decision" only when a genuine owner decision is required; '
        'put the question in "question" and the choices in "options".\n'
    )


def build_direct_launch(
    installation: object,
    *,
    worktree: Path,
    prompt: str,
    prompt_file: Path,
    schema_file: Path,
    final_file: Path,
    max_model_steps: int,
) -> DirectLaunch:
    """Build the locked per-client argv; executable comes from the manifest entry."""

    client = str(getattr(installation, "client", ""))
    executable = str(getattr(installation, "executable", ""))
    model = str(getattr(installation, "model", ""))
    effort = getattr(installation, "effort", None)
    if client == "muse_code":
        argv = [
            executable, "exec", "--json", "--prompt-file", str(prompt_file),
            "--workspace", str(worktree), "--yolo", "--model", model,
        ]
        if effort:
            argv += ["--reasoning-effort", str(effort)]
        argv += ["--max-model-steps", str(max_model_steps), "--output-schema", str(schema_file)]
        return DirectLaunch(argv=tuple(argv))
    if client == "antigravity_cli":
        frame = json.dumps(
            {"event": "user", "message": {"content": prompt}}, allow_nan=False
        )
        return DirectLaunch(
            argv=(
                executable, "--print", "--input-format", "stream-json",
                "--output-format", "stream-json", "--dangerously-skip-permissions",
                "--model", model, "--disable-slash-commands",
            ),
            stdin_data=(frame + "\n").encode(),
        )
    if client == "codex_app_server":
        return DirectLaunch(
            argv=(
                executable, "exec", "--json", "-C", str(worktree),
                "-s", "workspace-write", "--dangerously-bypass-approvals-and-sandbox",
                "-m", model, "-o", str(final_file), "-",
            ),
            stdin_data=prompt.encode(),
            final_file=final_file,
        )
    if client == "claude_code":
        return DirectLaunch(
            argv=(
                executable, "-p", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "bypassPermissions", "--model", model,
            ),
            stdin_data=prompt.encode(),
        )
    raise ValueError(f"unsupported direct client: {client}")


__all__ = [
    "RESULT_SCHEMA",
    "SUPPORTED_DIRECT_CLIENTS",
    "TERMINAL_RUN_STATES",
    "DirectLaunch",
    "DirectResult",
    "DirectRunConflict",
    "DirectRunNotFound",
    "DirectRunRecord",
    "DirectRunState",
    "DirectSessionRecord",
    "DirectSessionState",
    "build_direct_launch",
    "build_direct_prompt",
    "parse_direct_result",
]
