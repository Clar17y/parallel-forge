# Task 12 review-fix round-5 report

Commit SHA: supplied in the controller handoff; this report is included in the focused repair commit.

## Red evidence

Before the production edit, the new blocking-first-artifact regression produced:

```text
1 failed, 28 deselected
```

The outer Docker runner task became done immediately after cancellation while the first artifact
write remained blocked. This was the expected failure: direct awaiting allowed cancellation to
abort the evidence operation before stdout and stderr envelopes were both persisted.

## Green evidence

- New regression: `1 passed in 0.25s`.
- Docker runner file: `29 passed` in the implementer run.
- Focused runner suite (`test_runner_policy.py`, `test_docker_runner.py`, and
  `test_host_runner.py`): `56 passed in 6.82s`.
- Ruff check on touched files: passed.
- Ruff format check on touched files: `2 files already formatted`.
- mypy `apps/orchestrator/src/forge`: `Success: no issues found in 102 source files`.
- `git diff --check`: passed.

## Evidence cancellation lifecycle

- Docker now treats the existing `persist_output_artifacts(...)` call as one bounded operation
  under `await_deferred_cancellation`.
- Cancellation delivered during either sequential artifact write is recorded and deferred while
  both deterministic, redacted stdout/stderr envelopes finish persisting.
- After complete persistence, pending cancellation propagates before `CommandResult`
  construction. Repeated cancellation cannot interrupt the evidence boundary.
- An artifact adapter failure still crosses as the generic `RunnerExecutionError`; no cleanup,
  trusted-host, policy, output-schema, or public request/result behavior changed.

Touched files are limited to `apps/orchestrator/src/forge/tools/docker.py` and
`apps/orchestrator/tests/tools/test_docker_runner.py`. Scoped independent re-review and fresh full
verification remain outstanding.
