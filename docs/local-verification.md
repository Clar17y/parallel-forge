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
overrides that selection's pytest step timeout (a selection with no pytest step
rejects the flag rather than silently ignoring it), and `--image REF` reuses an
existing controller image instead of building the pinned one.

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

- Python 3.14 is verified before any step runs. Node.js 24 is verified for the host
  selections that invoke it; the controller selections use the Node shipped in
  `Dockerfile.verify`, whose build asserts the pinned major version. Dependencies
  are resolved with `uv sync --frozen` / `npm ci` from the committed locks.
- Container selections run in `Dockerfile.verify`, an image whose dependencies are
  baked from `uv.lock` at build time, so a recorded run performs no package
  resolution and needs no outbound network.
- The controller container is started with `--init` and is checked at run time for
  UID/GID 1000, an init reaper as PID 1, Python 3.14, the mounted `uv.lock` and an
  empty `/workspace/.env`. UID 1000 is deliberately distinct from the Forge sandbox
  identity 10001 in `Dockerfile.runner`, which keeps private-stage and ACL fixtures
  valid. A mismatch fails the run before any test executes.
- The harness does not bind the host repository root directly to `/workspace`. Instead,
  it builds a deterministic workspace projection from Git candidate inputs (tracked
  plus nonignored untracked files) and binds existing top-level candidate entries to
  their corresponding `/workspace/<name>` paths. This keeps `/workspace` itself
  container-owned so mounting the empty mask at `/workspace/.env:ro` cannot create or
  alter a host `repo_root/.env` mountpoint.
- The repository-root `.env` and other root `.env.*` secret variants are explicitly
  excluded from projection, while preserving the tracked `.env.example` fixture. Local
  ignored state (such as `.git` metadata, `.llm-output` evidence, virtual environments,
  and local caches) and root dotenv secrets are not mounted. An empty read-only file is
  mounted over `/workspace/.env:ro` for every controller invocation (both the identity
  probe and the test runner). Local development secrets from an ignored repository-root
  `.env` never enter argv, logs, records or container-visible files, and the harness
  does not materialize a host `.env` mountpoint. Ignored files within projected top-level
  directories remain readable through those directory binds; runtime Settings loads
  only the root file.
- PostgreSQL 17 (pinned by repository digest) runs as an ephemeral container with
  no network, sharing only the
  controller's network namespace. The fixtures' fixed loopback endpoint
  (`127.0.0.1:5435`) therefore stays valid while the controller keeps no outbound
  network and no Docker socket. Readiness is polled through `pg_isready` with a
  bounded attempt count and timeout, and the observed attempts are recorded.

Evidence and policy:

- Candidate identity records the HEAD commit, branch, dirty flag, index/tracked
  tree digest and an aggregate working-tree digest sensitive to the actual bytes of
  dirty tracked and untracked files. Raw bytes are hashed safely with NUL path
  framing, and file contents or secrets are never persisted in evidence records.
- Every command is recorded as an argv list with its exit code, duration, timeout,
  resolved immutable image IDs and raw stdout/stderr log paths, plus JUnit output
  for pytest selections. Recorded output and error lines pass through the
  repository's `redact_secrets`, and the recorded PostgreSQL argv carries a
  redacted password, so evidence never publishes a credential-shaped value.
  Container pytest writes JUnit output to container storage (`/tmp`), and the
  harness extracts it to the documented host evidence directory as a recorded
  `docker cp` step so runs succeed even when the host-created evidence directory is
  not writable by UID 1000, without weakening host permissions. Missing, empty, or
  failed artifact extraction is treated honestly as an environment error rather
  than a test outcome, even when pytest failed.
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
  blocked by the shutdown signal that cancelled the selection. A failed individual
  `docker rm` is recorded as a note rather than a verdict, because the post-removal
  sweep is what actually proves absence; the sweep and daemon failures stay fatal.
- The run record states which containers were removed and the exact removal and
  verification commands. A surviving container makes the run exit non-zero even if
  the tests passed, and it outranks a cancellation verdict. Host selections record
  `cleanup.applicable: false` with `verified_absent: true` rather than implying a
  cleanup that never applied to them.

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
`2` configuration error, `3` environment error — including a step timeout, a
launch failure or an owned container that survived cleanup — and `130` cancelled.
The runner's non-exit sentinels are classified separately from pytest exit codes,
so a timed-out suite is never recorded as a failing suite, and a surviving owned
container outranks the cancellation verdict in the record.

## Known limits

- The controller image reuses the repository's approved immutable base pins
  (`python:3.14.2-slim`, `node:24.19.0-slim`), which is why a container run reports
  Python 3.14.2 and Node 24.19.0 while hosted CI uses 3.14.5 and 24.20.0. The
  recorded versions make that difference visible rather than implicit; a
  patch-level comparison against CI requires the host selections.
- The PostgreSQL base is pinned by repository digest for the same reason. Refresh
  it deliberately with
  `docker image inspect postgres:17 --format '{{index .RepoDigests 0}}'` and update
  `POSTGRES_IMAGE`, then re-run one container selection so the recorded image
  identity matches the pin.
- Host selections run on the host platform, so they inherit its OS semantics.
  They are the local equivalents of the workflow steps, not a cross-platform
  matrix.
- `docker`-marked runner contract tests and Chromium/browser acceptance are not
  selections here; they stay separate, explicitly provisioned checks.
- When the harness was introduced, the `unit` and the focused `backend` selection
  above were executed end to end. The `web` selection and the broad `backend`
  selection were not; their step lists mirror the workflow commands and are
  recorded as unexercised until an operator runs them.
- The shared subprocess runner buffers child output and discards it when a step
  exceeds its timeout, so a timed-out step currently records an empty log with an
  accurate `timed_out` outcome. The outcome is trustworthy; the partial output is
  not available. Streaming that output would require changing `scripts/dev.py` for
  every caller and is deliberately left as separate work.
- `controller_image.id` is the authoritative image identity for a run, not the
  `inputs_digest` that names the tag: the image installs `build-essential`, `git`,
  `ripgrep` and `uv` from floating sources, so two machines can hold different
  images under the same tag. Compare `controller_image.id` when results disagree.
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
require `--full`, and its pytest targets. Host selections may also declare fixed
setup steps; a container selection that declares them is rejected, because the
container path does not execute them and silently skipping setup would run the
selection against an unprepared controller. The contract tests
in `tests/integration/test_verify_harness.py` cover argument validation, evidence
recording and the cleanup invariants for every selection; they run with the other
development-script tests in the broad `backend` selection, or directly with
`uv run --frozen python -m pytest tests/integration/test_verify_harness.py -q`.
