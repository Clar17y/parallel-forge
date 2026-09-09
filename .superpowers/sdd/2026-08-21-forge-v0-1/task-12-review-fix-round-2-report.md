# Task 12 review-fix round-2 report

Commit SHA: supplied in the final handoff; this report is included in the focused repair commit.

## Red evidence

The new deterministic delayed-launch regression was run before the production change:

```text
.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/tools/test_docker_runner.py::test_docker_cancellation_waits_for_delayed_launch_before_cleanup -q
1 failed in 0.56s
```

The pre-fix run attempted `docker rm -f` while the launch worker was blocked before creating
the named container, then finished with `RunnerExecutionError`; releasing the launch afterward
left the container launched without a remaining cleanup path.

## Green evidence

- Focused runner suite (`test_runner_policy.py`, `test_docker_runner.py`, and
  `test_host_runner.py`): `52 passed in 5.56s`.
- Ruff check on touched files: passed.
- Ruff format check on touched files: `2 files already formatted`.
- mypy `apps/orchestrator/src/forge`: `Success: no issues found in 102 source files`.
- `git diff --check`: passed.

## Delayed-launch lifecycle

- The primary bounded `ProcessRunner` Docker launch now runs through
  `await_deferred_cancellation`, so caller cancellation is recorded while the launch worker
  reaches its terminal result under the policy timeout.
- Only after that launch result is terminal does Docker force-remove the exact generated name.
  Repeated cancellation remains deferred through both launch completion and cleanup; cleanup
  timeout/failure remains the generic `RunnerExecutionError` and outranks cancellation.
- Existing adapter-exception, timeout, exit-125, output-artifact, exact-argv, image, and
  containment behavior remains covered by the focused suite.
- Host-runner semantics were intentionally left unchanged.

Touched files: `apps/orchestrator/src/forge/tools/docker.py` and
`apps/orchestrator/tests/tools/test_docker_runner.py`.

Unresolved concern: fresh independent re-review and controller verification remain outstanding;
Task 12 is not claimed complete here.
