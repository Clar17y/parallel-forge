# Task 12 review-fix round-4 report

Commit SHA: supplied in the controller handoff; this report is included in the focused repair commit.

## Red evidence

Before the production change, the deterministic daemon-visibility regression failed on the
first nonzero cleanup attempt:

```text
.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/tools/test_docker_runner.py::test_docker_timeout_retries_exact_name_cleanup_until_daemon_visibility -q
1 failed in 0.62s
```

The model made the same named container visible only after the first `docker rm -f` returned
"not found"; the pre-fix runner raised immediately and never issued a second removal.

## Green evidence

- New daemon-visibility regression: `1 passed in 0.47s`.
- Retry-success plus retry-exhaustion checks: `2 passed in 1.04s`.
- Focused runner suite (`test_runner_policy.py`, `test_docker_runner.py`, and
  `test_host_runner.py`): `55 passed in 6.87s`.
- Ruff check on touched files: passed.
- Ruff format check on touched files: `2 files already formatted`.
- mypy `apps/orchestrator/src/forge`: `Success: no issues found in 102 source files`.
- `git diff --check`: passed.

## Cleanup retry boundary

- Every terminal cleanup uses the same exact generated container name; it never enumerates or
  removes another resource.
- Cleanup makes at most three attempts. Each attempt is independently bounded to 15 seconds and
  retries are separated by 0.25 seconds, so the worst-case cleanup duration is bounded to 45.5
  seconds plus fixed local scheduling overhead.
- Adapter errors, cleanup timeouts, and nonzero Docker results remain uncertain and consume the
  same retry budget. Only a non-timed-out zero result proves successful removal.
- Exhaustion raises the generic `RunnerExecutionError`; it cannot return a timed-out command
  result or allow pending caller cancellation to outrank cleanup failure.
- Tests prove delayed visibility succeeds on a later exact-name attempt and persistent failure
  exhausts all three attempts against that same name.

Touched files are limited to `forge/tools/docker.py` and its focused tests. Host and shared
cancellation semantics are unchanged. Fresh independent re-review and verifier evidence remain
outstanding.
