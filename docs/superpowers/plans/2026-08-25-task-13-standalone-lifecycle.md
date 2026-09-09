# Task 13 Slice G: Standalone Developer Lifecycle Implementation Plan

> **Execution rule:** implement each numbered task as a red-green-refactor cycle, commit the completed Slice G candidate, obtain a fresh independent read-only review, repair findings with focused regression tests, then run exact-head verification before updating the Task 13 ledger.

**Goal:** Add recoverable `forge worktree setup` and `forge worktree teardown` commands plus thin PowerShell/Bash wrappers, without creating a persisted run, operation intent, event, or run resource row.

**Architecture:** A standalone lifecycle service owns orchestration and a versioned, owner-protected manifest. It resolves the one registered project matching the canonical current repository, derives `WorktreeIdentity.for_developer`, and delegates only exact-identity effects to `ControlledGit`, `DatabaseProvisioner`, `EnvironmentStager`, and `WorktreeRunnerFactory`. Manifest checkpoints replace run events for recovery. Teardown requires that exact manifest, removes the worktree before the database, retains the branch, and deletes the manifest only after verified cleanup.

**Security invariants:** no scan-based recovery; no arbitrary Git ref resolution; no run-scoped operation record; no secret value, administrator URL, scoped URL, environment content, or command output in the manifest or CLI diagnostics; atomic manifest replacement; unsafe links/reparse points and foreign manifests fail closed; disabled database setup makes zero administrator/secret calls.

---

## Task 1: Secure standalone manifest

**Files:**
- Create: `apps/orchestrator/src/forge/tools/worktree_manifest.py`
- Create: `apps/orchestrator/tests/tools/test_worktree_manifest.py`

1. Write failing tests for a versioned manifest containing only project UUID, canonical repository path, full branch, worktree identity/path, base SHA, policy version, database state/name/role, opaque secret ID, and completed checkpoints.
2. Add tests proving the filename uses the full SHA-256 branch digest; sanitizing collisions do not alias; malformed/foreign fields fail; secret-like extra fields are rejected; an existing different manifest is not overwritten.
3. Add filesystem tests for atomic publication, owner-only POSIX mode/Windows ACL, safe same-content replacement, exact read/update/delete, and refusal of manifest-root or target symlinks/reparse points.
4. Run `python -m pytest apps/orchestrator/tests/tools/test_worktree_manifest.py -q` and observe the missing module failure.
5. Implement immutable Pydantic manifest/checkpoint models and `WorktreeManifestStore` with bounded canonical JSON, no-follow validation, atomic replace, directory durability, and stable redacted exceptions.
6. Re-run the focused tests and Ruff for the two files.

## Task 2: Developer-safe exact adapters

**Files:**
- Modify: `apps/orchestrator/src/forge/application/ports/worktrees.py`
- Modify: `apps/orchestrator/src/forge/tools/git.py`
- Modify: `apps/orchestrator/src/forge/tools/database.py`
- Modify: `apps/orchestrator/src/forge/tools/environment.py`
- Modify: `apps/orchestrator/src/forge/tools/worktree_runner.py`
- Modify: `apps/orchestrator/tests/tools/test_git.py`
- Modify: `apps/orchestrator/tests/tools/test_database_provisioner.py`
- Modify: `apps/orchestrator/tests/tools/test_environment_staging.py`
- Modify: `apps/orchestrator/tests/tools/test_worktree_runner.py`

1. Write failing tests for `ControlledGit.resolve_default_base_sha()` returning only the exact local default-branch commit through fixed arguments and redacted failure handling.
2. Write failing tests for standalone database provision/inspect/rematerialize/teardown methods using `WorktreeIdentity.for_developer`; verify no operation repository/executor call, exact branch-derived secret ID, partial-state reconciliation, disabled-path zero calls, scoped URL transience, and exact absence verification.
3. Write failing tests allowing developer identities through environment staging and worktree-bound runner validation while preserving project/path/policy/database containment and rejecting forged identities.
4. Implement the smallest shared identity/secret-ID helpers and standalone database methods around the existing inspection-first adapters. Keep persisted-run methods and their durable-intent validation unchanged.
5. Implement fixed default-branch resolution and developer identity validation in staging/runner boundaries.
6. Run the four focused test modules and Ruff/mypy for touched modules.

## Task 3: Resumable standalone orchestration

**Files:**
- Create: `apps/orchestrator/src/forge/tools/developer_worktree.py`
- Create: `apps/orchestrator/tests/tools/test_developer_worktree.py`

1. Write failing unit tests with spies for exact setup ordering: manifest initialized before the first mutation, worktree create/verify, optional database provision/verify, environment stage/inspect, then policy-ordered bootstrap/install/migration/seed commands.
2. Cover disabled database zero calls, `--no-bootstrap` semantics, nonzero/timeout command failure, redacted diagnostics, and final exact verification.
3. Cover restart at every checkpoint: existing exact worktree, active database, staged environment, and individual completed setup commands. Checkpoints identify commands by ordinal plus immutable command digest; a changed policy/version or manifest identity fails closed.
4. Write teardown tests for manifest-required identity revalidation, worktree-first/database-second order, exact absence checks, branch retention, partial database failure, retry, disabled database, repeated teardown while the manifest remains, and manifest deletion only after complete verified absence.
5. Implement `DeveloperWorktreeLifecycle` with injected ports and short manifest updates after every verified effect. Never infer ownership from a directory/database scan.
6. Run the focused module and the affected lifecycle suite; run Ruff/mypy.

## Task 4: Project resolution, CLI, and thin wrappers

**Files:**
- Create: `apps/orchestrator/src/forge/cli/worktrees.py`
- Modify: `apps/orchestrator/src/forge/cli/main.py`
- Create: `scripts/setup-worktree.ps1`
- Create: `scripts/setup-worktree.sh`
- Create: `scripts/teardown-worktree.ps1`
- Create: `scripts/teardown-worktree.sh`
- Create: `tests/integration/test_worktree_scripts.py`

1. Write failing Typer/process tests for `worktree setup --branch`, `--no-bootstrap`, `worktree teardown --branch`, interactive confirmation, decline-as-success/no mutation, `--yes`, stable nonzero redacted failures, and secret-free stdout/stderr.
2. Test registered-project resolution by canonical current repository path: exactly one matching project/current policy is required; reconstruct `ProjectPolicy` from the immutable stored document and identity fields; dispose the engine before lifecycle effects.
3. Add a narrowly scoped environment-backed administrator-secret resolver for `secret://environment/NAME`; validate the reference name, read only that exact variable inside the trusted database adapter, and never include its value in object representations or errors. Database-disabled setup must not construct or call it.
4. Assemble `CanonicalRoot`, `ControlledGit`, `LocalSecretStore`, `DatabaseProvisioner`, `EnvironmentStager`, and `WorktreeRunnerFactory`. Resolve Git via an absolute trusted executable and use `Settings.data_root` for isolated state/manifests/secrets.
5. Implement PowerShell wrappers with terminating errors and exact exit propagation, and Bash wrappers with `set -euo pipefail` plus `exec`. Wrappers only forward arguments and contain no lifecycle or secret logic.
6. Exercise the Python CLI and host wrapper against a temporary registered, database-disabled Git project, including paths with spaces. Add static and process checks for forwarding, exit propagation, confirmation, and absence of secret material. CI supplies the opposite-shell coverage.
7. Run `python -m pytest tests/integration/test_worktree_scripts.py -q`, focused CLI tests, Ruff, and mypy.

## Task 5: Candidate review and Task 13 completion evidence

**Files:**
- Modify: `.superpowers/sdd/2026-08-21-forge-v0-1/progress.md`
- Modify only if a decision changed: `.superpowers/sdd/2026-08-21-forge-v0-1/task-13-decisions.md`

1. Run the complete affected lifecycle suite and inspect the diff for secret leakage, unsafe path handling, fabricated durable records, and branch deletion.
2. Commit Slice G as one reviewable candidate and request a fresh independent read-only specification/quality/test review against the design, this plan, and exact candidate commit.
3. For every accepted finding, add a failing regression test, implement the narrow repair, rerun focused checks, and request re-review of the new exact commit.
4. Run exact-head verification: full pytest suite with a fresh repository-local temp root, Ruff, strict mypy, CLI process tests, host wrapper tests, and PostgreSQL integration tests when the configured administrator database is available.
5. Update the progress ledger with exact commits, test counts, skipped integration rationale, review verdict, known unrelated flakes, and Task 13 completion/carry-forwards. Commit the ledger separately and push `forge/v0-1`; do not create or merge a PR.
