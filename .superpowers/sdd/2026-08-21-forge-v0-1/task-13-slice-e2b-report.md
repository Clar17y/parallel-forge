# Task 13 Slice E2b report

## Scope and base

E2b only: bind the existing Docker and explicitly trusted-host command
adapters to one retained managed worktree, expose terminal command evidence
before cancellation propagation, and prove the fixed-UID Docker read path.
E2c setup-command orchestration, durable step checkpoints, final preparation
evidence, teardown, CLI/wrapper behavior, push, PR, and merge remain deferred.

Implementation started from the required base
`6aef7c0228a24bd0d7f5a37cdee6d62f37b238fb`.

## RED-first evidence

Before production edits, the new focused test was run:

```text
.\\.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/tools/test_worktree_runner.py -q
```

It failed during collection with:
`ImportError: cannot import name 'CommandTerminalResult' from forge.application.ports.runner`.

## Review repairs and RED/GREEN evidence

The independent review identified three narrow E2b repairs. Each production
repair remained within the existing runner/port/smoke boundaries:

- Trusted-host cancellation during a blocked attempt audit initially launched
  one process after the audit returned with cancellation observed. The RED
  command was:

  ```text
  .\\.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/tools/test_host_runner.py -q -k attempt_audit
  1 failed, 1 passed, 4 deselected
  ```

  The failing assertion observed one `('lint', '--check')` process call where
  zero was required. `TrustedHostRunner` now raises `CancelledError` only
  after the deferred attempt-audit operation completes and before any launch.
  Audit failure still wins over cancellation. The repair regression passes.
- The factory protocol's return type exposed only `RunnerPort`, so E2c could
  not type-call `run_terminal`. The RED command:

  ```text
  .\\.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/tools/test_worktree_runner.py -q -k factory_contract
  ImportError: cannot import name 'WorktreeRunnerPort' from forge.application.ports.runner
  ```

  `WorktreeRunnerPort` now combines compatibility and terminal methods,
  `WorktreeRunnerFactoryPort.create` returns it, and the regression checks
  both the static annotation and runtime `isinstance` contract.
- The real smoke reader now explicitly asserts `Path.cwd() == /workspace`,
  `os.getuid() == 10001`, and `/workspace/.git` is a file (the linked
  worktree discriminator; the repository root has a `.git` directory). It
  reads the staged file, prints only `FORGE_E2B_OK`, and the host assertion
  requires an empty stderr artifact. This strengthened Docker path could not
  produce a local RED because the sandbox Docker daemon is inaccessible;
  the post-repair supported-host run is delegated to the root verifier.

## Implemented contracts and mechanics

- Added immutable, slots-based `CommandTerminalResult` with exactly
  `result: CommandResult` and `caller_cancelled: bool`, including type and
  exact-boolean validation. Added `TerminalRunnerPort`, the combined
  `WorktreeRunnerPort`, and the narrow `WorktreeRunnerFactoryPort`; `RunnerPort.run`
  remains unchanged.
- Added `WorktreeRunnerFactory` and `WorktreeBoundRunner`. Factory creation
  accepts only a `ManagedWorktree` and exact immutable `ProjectPolicy`, reuses
  the `CanonicalRoot` owned by `ControlledGit`, and has no filesystem or
  process effect. It rejects wrong repository/project identity, database
  shape, runner mode/trust/image setup, and non-run identities through stable
  redacted errors.
- Each bound invocation opens a fresh mutation-locking
  `ControlledGit.open_worktree_capability`, retains it through process or
  container completion, cleanup, artifact persistence, trusted-host completion
  audit, terminal result construction, and final revalidation, then releases
  it. Capability failure at open, launch, or release fails closed without a
  fabricated result.
- Existing Docker and trusted-host runners now implement `run_terminal`; the
  compatibility `run` delegates to it and raises `CancelledError` only after
  terminal work when the marker is true. Repeated cancellation is deferred
  through process/container cleanup, both redacted artifacts, completion
  audit, immutable result construction, and capability release.
- Managed Docker commands retain the capability-derived worktree source at
  `/workspace`, use exactly one bind mount and `/workspace` workdir, and keep
  the existing pinned image, fixed UID `10001:10001`, network, resource,
  read-only-root, tmpfs, no-new-privileges, dropped-capability, and exact
  named-command controls. On POSIX the mount source is the retained
  `/proc/<forge-pid>/fd/<fd>` capability path; on Windows it is the exact
  canonical handle-backed path. The Docker client process is also launched
  from the capability-derived cwd.
- Managed Docker names use a fresh UUID and an unguessable Forge ownership
  label token. Cleanup queries the exact name, accepts only Docker's explicit
  exact-name absence response, requires the label to match, removes the
  captured immutable container ID, and verifies that same ID is absent.
  Ambiguous queries and foreign same-name replacements fail closed without
  removing the foreign container. The token, source path, argv, environment
  values, output, and adapter errors do not enter results, reprs, telemetry,
  audit payloads, or stable errors.
- Trusted-host bound commands run the exact policy argv from the retained
  worktree cwd only when `TRUSTED_HOST` and `trusted_project=True` are both
  present. Attempt and completion audits remain high priority; the completion
  evidence digest is bound to the immutable `CommandResult`, and the
  cancellation marker follows the completion-audit cutoff rule.
- Both modes detach only exact resolved-command environment keys, reject
  malformed, oversized, NUL-containing, extra, and runner-control values,
  and redact selected values before storing stdout/stderr envelopes.

## Verification

Focused E2b runner evidence:

```text
.\\.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/tools/test_docker_runner.py apps/orchestrator/tests/tools/test_host_runner.py apps/orchestrator/tests/tools/test_worktree_runner.py -q
41 passed in 14.17s
```

Affected runner, process, policy, Git, worktree, environment staging,
database, path containment, repository inspection, artifact, redaction,
telemetry, and E2b suites:

```text
535 passed, 36 skipped in 245.84s (0:04:05)
```

Configured full pytest suite:

```text
1443 passed, 54 skipped in 366.83s (0:06:06)
```

Candidate static checks after the final code change:

```text
Ruff check (final repair-touched implementation/test/integration files): All checks passed!
Ruff format --check: 5 repair-touched files already formatted
Mypy apps/orchestrator/src/forge: Success: no issues found in 111 source files
git diff --check: passed
```

The repository-wide Ruff scan also exited successfully; its sandbox scan
reported one unrelated access-denied warning while still returning
`All checks passed!`, so the candidate-scoped check above is the authoritative
static result.

The root agent's exact Docker evidence on commit `8aa7541` (before these
review repairs) was run with the required escalated daemon access:

```text
tests/integration/test_runner_container.py -q -m docker
2 passed in 10.47s
```

That pre-repair run included the existing pinned-toolchain smoke and the
managed worktree E2a staged-file smoke. The latter read the staged file as
fixed numeric UID `10001`, used `/workspace`, exited zero with the fixed
success marker, and verified the secret was absent from output/artifacts and
terminal representation. It is recorded as pre-repair evidence only; the
strengthened UID/linked-worktree/empty-stderr assertions require the root's
post-repair run. My sandbox-only attempt of the same module reported
`2 skipped` because Docker's named pipe was inaccessible (`Access is denied`);
this is a local permission limitation, not a passing claim.

WSL/POSIX verification was attempted with the worktree's WSL distribution;
the platform service returned `WSL/Service/E_ACCESSDENIED`, so no WSL result
is claimed. The real Docker smoke above is the available supported-host
descriptor/mount proof.

## Compatibility and remaining dependencies

Existing repository-root Docker and trusted-host callers remain on their
original construction path and their full test suites remain green. E2b adds
the capability-bound factory path without changing root-runner call sites.

E2c still owns setup-command intent/checkpoint sequencing, BOOTSTRAP/INSTALL/
MIGRATION/SEED orchestration, terminal step evidence persistence, final
`resource.worktree_prepared` evidence, cancellation-aware workflow recovery,
and the later run transition. No E2c orchestration or schema work was changed
here.
