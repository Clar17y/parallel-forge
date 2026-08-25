# Task 13 Remaining Lifecycle Design

**Status:** Approved for implementation on 2026-08-25.

**Scope:** Finish Task 13 after Slice E2c by adding persisted-run teardown/reconciliation and the standalone developer worktree CLI with thin cross-platform wrappers. This specification refines, but does not expand, the approved Forge v0.1 design, Task 13 plan, and Task 13 controller decisions.

## Goals

- Remove only the exact worktree and optional database resources already attributed to one identity.
- Preserve truthful durable state when cleanup is interrupted or only partly succeeds.
- Keep managed branches by default; branch deletion is outside this Task 13 slice.
- Provide a standalone developer setup/teardown path that does not require or fabricate a persisted run.
- Exercise the Python CLI and both wrappers as processes, including confirmation and failure behavior.

## Non-goals

- No API or worker wiring; those consumers arrive in later tasks.
- No workflow transition from `PREPARING_WORKTREE`.
- No speculative rollback during setup.
- No general resource table or migration.
- No automatic branch deletion, remote mutation, PR creation, or merge.
- No broad cleanup by branch slug, directory scan, database prefix, or stale-resource guesswork.

## Slice F: Persisted-run teardown and reconciliation

`WorktreeProvisioner` gains an explicit persisted-run teardown operation. The caller supplies the run identity and authoritative project policy. The service reloads the locked run, recomputes `WorktreeIdentity.for_run`, and rejects any mismatch among project, branch, base SHA, registered path, database policy, and persisted database fields before starting a destructive effect.

Worktree cleanup and database cleanup are separate durable operation intents. Each intent is written before its first external effect and uses deterministic request identity so retries adopt or reconcile the same operation instead of issuing an unrelated mutation.

Cleanup order is fixed:

1. Validate the exact persisted resource identity.
2. Inspect and remove the exact registered managed worktree.
3. Verify that the worktree target and Git registration are absent.
4. Prune only stale Git worktree metadata through `ControlledGit.prune`.
5. For an enabled database resource, invoke `DatabaseProvisioner.teardown` with the exact persisted database/role/secret identity.
6. Persist `ResourceState.REMOVED` with null database identity only after exact database teardown succeeds.
7. For `DISABLED`, make zero administrator, database, resolver, or secret-store calls and preserve the disabled marker.

The branch is retained throughout. A missing exact worktree is an idempotent success only when inspection proves both the target and its registration absent. A foreign registration, wrong path, wrong branch, reparse point, ambiguous state, or identity mismatch fails closed without advancing to database cleanup.

If worktree removal succeeds but database cleanup fails, the durable worktree operation remains succeeded and the run retains its exact remaining database identity and nonterminal database state. A retry resumes at database cleanup without trying to recreate or remove unrelated resources. If cancellation arrives while a synchronous Git mutation is running, Forge awaits its terminal result, records any exact verified outcome, and only then propagates cancellation.

Reconciliation remains inspection-led. It may complete a durable teardown intent only when the exact intended absence or exact remaining state is observable. It never guesses ownership, repeats an unverified destructive effect, deletes a branch, or converts an ambiguous result into success.

## Slice G: Standalone developer lifecycle

The standalone path is implemented separately from `WorktreeProvisioner`. It uses `WorktreeIdentity.for_developer`, `ControlledGit`, `DatabaseProvisioner`, `LocalSecretStore`, environment staging, and the worktree-bound runner as appropriate, but it creates no run-scoped operation intent, event, or repository row.

Standalone setup resolves the registered project and policy, derives the exact branch-hash identity, prepares the managed worktree, optionally provisions its exact database, stages approved environment files, and runs the configured setup sequence. Before the first mutation it writes an owner-protected manifest under `Settings.data_root / "worktrees"`. The manifest filename is derived from the full branch digest rather than a sanitized branch name.

The versioned manifest contains only non-secret identity and recovery data: project UUID, canonical repository path, full branch, worktree name and path, base SHA, database state, database name, database role, opaque secret ID, policy version, and completed lifecycle checkpoints. It never contains secret bytes, administrator credentials, the scoped database URL, environment-file contents, or command output. Publication is atomic; an existing different manifest or unsafe link/reparse point fails closed.

Standalone teardown requires the manifest and recomputes the developer identity before any mutation. It follows the same exact worktree-first/database-second order as Slice F, updates the manifest after each verified checkpoint, and removes the manifest only after all owned resources are verified absent or the database is verified `DISABLED`. Repeated teardown is safe while that exact manifest remains available. There is no discovery-by-scan fallback.

## CLI and wrappers

The Typer CLI adds a `worktree` command group with `setup` and `teardown` commands. Both commands use stable redacted errors and nonzero exit codes on failure. Teardown prompts for confirmation unless `--yes` is supplied. Declining confirmation performs no mutation and exits successfully with a clear cancellation message. Branches are always retained in Task 13.

The repository-root scripts are thin argument-forwarding processes:

- `scripts/setup-worktree.ps1`
- `scripts/setup-worktree.sh`
- `scripts/teardown-worktree.ps1`
- `scripts/teardown-worktree.sh`

They select the repository's Forge CLI module, preserve exit status, introduce no lifecycle logic, and do not print secrets. PowerShell uses strict terminating-error behavior; Bash uses `set -euo pipefail` and `exec`.

## Testing strategy

Implementation follows strict red-green-refactor cycles.

Slice F tests cover exact identity validation, worktree-first ordering, disabled-database zero calls, active and partial database cleanup, retry after each interruption point, cancellation, wrong/foreign resource refusal, locked worktree behavior, repeated teardown, durable checkpoints, redacted failures, and branch retention.

Slice G tests cover manifest validation and atomicity, branch-hash collision resistance, standalone setup recovery, exact teardown, partial failure and retry, unsafe link/reparse-point refusal, confirmation/`--yes`, wrapper argument forwarding, exit-code propagation, paths containing spaces, and secret-free stdout/stderr. Process tests execute the Python CLI and the wrapper available on the host; CI supplies both PowerShell and Bash coverage.

Focused tests run after each red-green cycle. Before Task 13 is marked complete, Forge runs the affected lifecycle suite, the full orchestrator suite, Ruff, mypy, process-level wrapper tests, and PostgreSQL integration tests when the configured test administrator database is available.

## Review and carry-forwards

Slices F and G are separate commits and review gates. Because the user requested a single active Forge implementation agent, implementation and self-review stay in this task; the candidate is then packaged for an independent external review before Task 13 is marked complete.

The accepted E2c P3 carry-forwards remain explicit:

- Change the final prepared-event resource-version check from exact `+1` to strictly later if a future workflow allows intervening events at that boundary.
- Make environment-staging and runner dependencies mandatory when API/worker wiring lands.

Neither carry-forward changes Task 13's remaining lifecycle behavior today. The known intermittent schema-test infrastructure flake remains documented separately from Task 13 regressions.
