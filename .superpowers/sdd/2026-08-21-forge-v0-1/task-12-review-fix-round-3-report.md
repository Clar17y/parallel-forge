# Task 12 review-fix round-3 report

Commit SHA: supplied in the controller handoff; this report is included in the focused repair commit.

## Red evidence

Before the production change, the new parameterized regression produced `1 failed, 1 passed`:

```text
.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/tools/test_docker_runner.py::test_docker_launch_error_preserves_prior_cancellation_after_cleanup -q
```

The successful-cleanup case expected `CancelledError` but received `RunnerExecutionError` after a
cancelled, delayed launch terminated with `OSError`. The cleanup-failure case already returned
`RunnerExecutionError`, confirming the required fail-closed precedence.

## Green evidence

- New launch-error/cleanup regression: `2 passed in 0.22s`.
- Focused runner suite (`test_runner_policy.py`, `test_docker_runner.py`, and
  `test_host_runner.py`): `54 passed in 5.50s`.
- Ruff check on touched files: passed.
- Ruff format check on touched files: `3 files already formatted`.
- mypy `apps/orchestrator/src/forge`: `Success: no issues found in 102 source files`.
- `git diff --check`: passed.

## Cancellation-state design

- `DeferredCancellationState` is an explicit caller-owned state carrier. The shared bounded
  deferral helper marks it whenever caller cancellation is observed, before continuing to await
  the shielded operation.
- The state survives a later terminal operation exception, unlike the helper's success-only
  boolean return value.
- Docker supplies one state for the launch operation. After a launch exception, it performs
  exact-name cleanup; successful cleanup propagates prior caller cancellation, while cleanup
  timeout/failure still raises the generic `RunnerExecutionError` and outranks cancellation.
- Existing helper callers and trusted-host behavior retain their prior result/boolean contract.

Touched files are limited to `forge/tools/runner.py`, `forge/tools/docker.py`, and the Docker
runner tests. Fresh independent re-review and full verification remain outstanding.
