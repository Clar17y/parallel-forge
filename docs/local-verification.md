# Reproducible local verification

`scripts/verify.py` runs one explicit repository verification selection locally
and records its candidate identity, exact commands, outcomes and cleanup proof.
It exists because the v0.2 local Linux/PostgreSQL verification lived in ignored
`.llm-output/` scratch files from a single development session: the results were
kept, but the orchestration was not reproducible from the repository.

Hosted GitHub Actions remains the authority for CI. This harness is the supported
way to reproduce a deterministic selection on a workstation without dispatching a
workflow, and it never replaces the CI gate.

## Usage

```text
python scripts/verify.py list
python scripts/verify.py run unit
python scripts/verify.py run web
python scripts/verify.py run recovery
python scripts/verify.py run backend --focused tests/recovery_process/test_harness_waits.py
python scripts/verify.py run backend --full
```

`--focused TARGET [TARGET ...]` replaces a selection's default targets with exact
repository-relative pytest node IDs and may be repeated. `--timeout SECONDS`
overrides the step timeout, and `--image REF` reuses an existing controller image
instead of building the pinned one.

The broad `backend` selection is the repository's full deterministic backend suite
(roughly 80 minutes) and requires `--full`; a focused run never expands into it.

## Selections

| Selection | Environment | Scope |
| --- | --- | --- |
| `unit` | host | Domain, agent, observability and artifact suites plus the host-wrapper contracts; no PostgreSQL |
| `web` | host | Locked dependency install, OpenAPI/generated-type checks, web unit tests, type check, lint, production build and the tracked-generated-contract diff |
| `recovery` | controller container + isolated PostgreSQL | Worker crash/restart and harness-wait boundaries |
| `backend` | controller container + isolated PostgreSQL | Full deterministic backend selection; broad, requires `--full` |

Selections mirror the repository workflows' own deterministic commands. Tests
marked `live_provider`, `live_github` and `docker` are excluded from every
selection, matching the local verification boundary recorded in
[the v0.2 progress ledger](v0.2-progress.md#current-v02-status).

## What each run guarantees

Environment:

- Python 3.14 and Node.js 24 are verified before any step runs, and dependencies
  are resolved with `uv sync --frozen` / `npm ci` from the committed locks.
- Container selections run in `Dockerfile.verify`, an image whose dependencies are
  baked from `uv.lock` at build time, so a recorded run performs no package
  resolution and needs no outbound network.
- The controller container is started with `--init` and is checked at run time for
  UID/GID 1000, an init reaper as PID 1, Python 3.14 and the mounted `uv.lock`.
  UID 1000 is deliberately distinct from the Forge sandbox identity 10001 in
  `Dockerfile.runner`, which keeps private-stage and ACL fixtures valid. A mismatch
  fails the run before any test executes.
- PostgreSQL 17 runs as an ephemeral container with no network, sharing only the
  controller's network namespace. The fixtures' fixed loopback endpoint
  (`127.0.0.1:5435`) therefore stays valid while the controller keeps no outbound
  network and no Docker socket. Readiness is polled through `pg_isready` with a
  bounded attempt count and timeout, and the observed attempts are recorded.

Evidence and policy:

- Candidate identity is the HEAD commit plus aggregate digests of the tracked tree
  and the working-tree state, so a later reader can tell whether recorded results
  still describe the inputs.
- Every command is recorded as an argv list with its exit code, duration, timeout,
  resolved immutable image IDs and raw stdout/stderr log paths, plus JUnit output
  for pytest selections.
- A run never calls a live provider, probes provider quota, reserves budget,
  dispatches GitHub Actions, retries a failed step automatically or expands a
  focused selection. Those guarantees are written into every record.
- Host child processes receive a credential-filtered environment. Reserved names
  (provider keys, tokens, `FORGE_*` except `FORGE_E2E_*`, `DATABASE_URL`) are
  removed before execution; only the *names* of removed variables are recorded.
  The controller container inherits no host environment at all and is given only
  the explicit, non-secret variables needed to run pytest.

Cleanup:

- Owned containers are named from the run ID. Cleanup re-derives those names,
  removes every owned container, then lists all containers and fails the run if any
  name still carries the run prefix. Cleanup runs after success, failure,
  configuration error and cancellation, and uses a teardown path that cannot be
  blocked by the shutdown signal that cancelled the selection.
- The run record states which containers were removed and the exact removal and
  verification commands. A surviving container makes the run exit non-zero even if
  the tests passed.

## Evidence layout

Each run writes to `.llm-output/local-verification/<run-id>/`:

```text
run.json                                   # record described above
logs/01-<step>.stdout.log                  # raw step output
logs/01-<step>.stderr.log
junit-<step>.xml                           # structured pytest outcomes
```

`.llm-output/` is ignored, so run records never enter the candidate tree or change
the identity they describe. Exit codes are `0` passed, `1` selection failed,
`2` configuration error, `3` environment or cleanup error, and `130` cancelled.

## Known limits

- The controller image reuses the repository's approved immutable base pins
  (`python:3.14.2-slim`, `node:24.19.0-slim`), which is why a container run reports
  Python 3.14.2 and Node 24.19.0 while hosted CI uses 3.14.5 and 24.20.0. The
  recorded versions make that difference visible rather than implicit; a
  patch-level comparison against CI requires the host selections.
- Host selections run on the host platform, so they inherit its OS semantics.
  They are the local equivalents of the workflow steps, not a cross-platform
  matrix.
- `docker`-marked runner contract tests and Chromium/browser acceptance are not
  selections here; they stay separate, explicitly provisioned checks.
- When the harness was introduced, the `unit` and the focused `backend` selection
  above were executed end to end. The `web` selection and the broad `backend`
  selection were not; their step lists mirror the workflow commands and are
  recorded as unexercised until an operator runs them.
- The harness records evidence. It does not review, approve, publish or merge
  anything, and a passing local selection is not a CI pass.

## Preserved historical evidence

The harness promotes the existing local procedure; it does not replace the
recorded results of the earlier session. Those remain in
[the v0.2 progress ledger](v0.2-progress.md#current-v02-status) and
`.llm-output/root-v02-local-verification/`:

- the complete Linux deterministic selection at `0764852`, reported as
  **6,314 passed, 2 failed, 92 skipped** with 10 live/Docker deselections;
- the targeted correction of the two failures through the identical
  source/image supervisor follow-up with a Docker `--init` reaper, reported as
  **51 passed, 1 platform skip**;
- the Docker selection (**9 passed**), Chromium/API/worker restart case
  (**1 passed**), Windows affected coverage (**772 passed, 69 skips**), Windows
  supervisor recovery (**52 passed**), and the corrected quarantine selection
  (**five Linux passes**).

Counts overlap and are not summed, and the failed full run is retained rather than
replaced by the later targeted rerun.

## Adding a selection

Add one entry to `SELECTIONS` in `scripts/verify.py`: a `name`, a summary, whether
it runs on the host or in the controller container, whether it is broad enough to
require `--full`, any fixed setup steps, and its pytest targets. The contract tests
in `tests/integration/test_verify_harness.py` cover argument validation, evidence
recording and the cleanup invariants for every selection; they run with the other
development-script tests in the broad `backend` selection, or directly with
`uv run --frozen python -m pytest tests/integration/test_verify_harness.py -q`.
