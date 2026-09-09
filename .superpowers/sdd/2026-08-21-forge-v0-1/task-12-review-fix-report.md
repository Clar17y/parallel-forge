# Task 12 review-fix report

Commit SHA: supplied in the final handoff; this report is included in the focused repair commit.

## Red evidence

After strengthening the deterministic regressions with cleanup/process completion signals and
event-loop delivery markers, the targeted command failed with `2 failed, 9 passed in 10.29s`:

```text
.venv\Scripts\python.exe -m pytest \
  apps/orchestrator/tests/tools/test_docker_runner.py::test_docker_cancellation_waits_for_blocked_cleanup_before_propagating \
  apps/orchestrator/tests/tools/test_host_runner.py::test_trusted_host_cancellation_finishes_evidence_and_completion_audit \
  apps/orchestrator/tests/tools/test_docker_runner.py::test_docker_async_adapter_runs_registered_argv_for_every_step_kind -q
```

The failures were the meaningful pending-task assertions: a second cancellation interrupted
Docker cleanup and trusted-host process completion before the repair.

## Green evidence

- Focused runner suite (`test_runner_policy.py`, `test_docker_runner.py`, and
  `test_host_runner.py`): `51 passed in 5.50s`.
- Ruff check on all touched Python files: passed.
- Ruff format check on all touched Python files: `5 files already formatted`.
- mypy `apps/orchestrator/src/forge`: `Success: no issues found in 102 source files`.
- `git diff --check`: passed.

## Lifecycle decisions

- `await_deferred_cancellation` owns one bounded operation in a task, shields it from caller
  cancellation, records cancellation, and continues through repeated cancellation until the
  operation has a terminal result.
- Docker routes cancellation, adapter failure, timeout, and exit-125 cleanup through
  `_cleanup_for_terminal`; cleanup failures remain the generic `RunnerExecutionError` even when
  cancellation is pending.
- Trusted-host execution, output evidence persistence, and high-priority completion audit each
  use the same deferral helper. A cancellation observed before completion-audit submission adds
  the non-secret `caller_cancelled: true` marker before the audit is written.
- Completion-audit persistence is treated as the bounded terminal commit point. If cancellation
  is first delivered while that audit await is already in progress, the helper finishes the
  audit before propagating cancellation; the resulting record is not attempt-only, and the
  marker's absence truthfully means cancellation was not observed before that commit began.

Touched files are limited to the Docker/host adapters, the shared runner cancellation helper,
and their focused regression tests. Independent re-review and fresh controller verification
remain outstanding.
