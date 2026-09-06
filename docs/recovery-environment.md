# Development environment recovery — 6 September 2026

## Recovery workspace snapshot

These paths describe the recovery session before its worktrees are torn down.
After the recovery PR is merged, start from `C:\dev\parallel-forge` on
`forge/v0-1` and create a fresh managed worktree. Do not assume the historical
worktree or its isolated database still exists.

- Branch: `codex/continue-development`.
- Worktree: `C:\dev\parallel-forge\.worktrees\forge-c704c58f7fbc-codex-continue-developme-514069ca6ccb`.
- Base commit: `5b07abd1d326dd777dd8d1e6126d78b3db833ef4`.
- Created and resumed through the repository's `scripts/setup-worktree.ps1`.
- Forge project: `c704c58f-7fbc-490b-80a0-ceaaef4ed61d`; policy version 1;
  canonical repository `C:\dev\parallel-forge`; development base `forge/v0-1`.
- Verified manifest checkpoints: `manifest.created`, `worktree.created`,
  `database.active`, `environment.staged`, `setup.complete`.
- Isolated developer database is ACTIVE. Its scoped password remains in Forge's
  protected local secret store; no administrator URL is persisted in this file.

## Installed and running

- Python 3.14.5 through pyenv; Node 24.20.0 through Volta; npm 12.0.2.
- uv 0.12.10 and RTK 0.46.0; locked Python dependencies/dev extra in `.venv`.
- WSL 2.7.13, Virtual Machine Platform enabled, Windows restarted.
- Docker engine 29.7.2; `parallel-forge-postgres-1` healthy on loopback port 5435.
- Control database migrated through `20260822_0002`.
- Runner built from the repository's pinned Dockerfile:
  `sha256:1d03c60c4d88a06d1ad713e2a75d4a93f34eda476d8ea40e388fa204deef070f`.
  Non-root, network-disabled smoke returned Python 3.14.2.
- Worktree-local, Git-ignored `.env` contains only `FORGE_RUNNER_IMAGE`.

Activate `.venv\Scripts\Activate.ps1` from this worktree, or invoke
`.venv\Scripts\python.exe` explicitly. Use a fresh terminal for updated tool PATH.

## Setup repair

The first scripted setup retained its worktree and database but failed at
environment staging: `WorktreeCapability.revalidate` rejected standalone
identities because they have no run ID. The focused fix reconstructs exact
developer identities with `WorktreeIdentity.for_developer`, retains run identity
reconstruction, and preserves the remaining capability checks. Four regressions
cover successful standalone capabilities and rejection of mismatched/forged
identities. The script then resumed successfully using the final worktree's
editable package, without recreating its resources.

## Historical recovery verification baseline

- Final candidate HEAD remains `5b07abd1d326dd777dd8d1e6126d78b3db833ef4`.
- Tracked uncommitted diff SHA-256:
  `006FCB9C359D44ADB56B2C0346EC50A70825952BE314A7F24B3DB1C6CB0EEE9F`.
- Gemini implemented the focused fix and test repair. Fresh Claude Opus 5
  review and re-review found no remaining blockers; the separate Sol-high
  correctness gate passed the final candidate.
- Git suite: 97 passed, 5 skipped; worktree wrapper suite: 8 passed, 2 skipped.
- Full deterministic baseline: 1,530 passed, 57 skipped, 92 failed. Failures:
  88 state-engine, 3 run-service, 1 schema. The 88 state-engine failures were
  independently reproduced on the unchanged bootstrap checkout. None of these
  three failing source/test modules was changed by the setup repair.
- The full baseline preceded the final test-only forgery improvement;
  production code is identical. Final focused capability tests passed 4/4,
  and `git diff --check` passed. Ruff and strict mypy passed during verification.
- This is a usable development environment with a recorded failing baseline,
  not a claim that all existing implementation tests pass.

Implementation/review contracts, provider outputs, saved candidate diff and
independent verification logs were recorded in `.llm-output/` and are archived
under the local Forge data directory's `recovery-archives` before teardown.
The recovery PR includes the source/test repair and supplied AGENTS.md routing
instructions; raw provider logs and local environment files are not published.

## Scope and continuation

The registered policy retains Docker as the runner and provisions a separate
developer database. Its setup-command and environment-file lists are empty:
setup completion does not claim dependency commands, environment-file database
injection, or migrations inside the isolated developer database. Dependency
installation here and control-database migration were explicit bootstrap steps.
The current local Forge settings still target the control database by default;
tests create their own disposable databases. Configure a versioned development
policy before using the isolated database for application migrations.

Run lifecycle commands from the canonical repository with this worktree's venv
on PATH, the administrator reference resolved only in process memory, and the
immutable runner image configured. Do not point lifecycle commands at the
isolated developer database: their project registration is in the control DB.
Both the script-created development worktree and temporary
`codex/recovery-development` bootstrap worktree are scheduled for removal after
the recovery PR is merged and their evidence is archived. Branches are retained.
The API and worker were not launched; completing their workflow wiring is
subsequent work. See `docs/continue-v0.1-goal.md` for the continuation contract.

## Manifest publication failure recovery

For current implementation progress and verification, use
`docs/v0.1-progress.md`; the baseline above records the earlier recovery session.

Standalone manifests live in `worktrees` below the configured Forge data root.
Exclusive creation briefly gives the completed manifest and its random
`.manifest-<uuid>.tmp` staging entry the same file identity. Successful cleanup
removes the staging entry before creation reports success. Windows cleanup uses
the retained native handle with bounded retries.

If native cleanup permanently fails, Forge returns a redacted error and may
retain the exact final payload plus its staging hard link. The final file then
has two links, so `exists`, `load`, `save`, and `delete` fail closed. The
developer-worktree coordinator propagates a failing `exists` call; it does not
treat the error as an absent manifest or start fresh provisioning. Repeatedly
retrying setup does not repair this condition.

Manual recovery requires the underlying filesystem error to be resolved first:

1. Stop processes using this Forge data root and preserve an evidence copy of
   the affected manifest and staging entries in a protected recovery archive.
2. Inspect native file identities and enumerate hard links. Confirm that the
   intended final manifest and exactly one staging entry are the same file,
   that both entries are inside this data root's `worktrees` directory, and that
   the manifest's project, branch, repository, base and policy match the expected
   registered worktree. Matching bytes alone do not establish file identity.
3. Only after those checks, remove the confirmed extra staging directory entry.
   Retain the final manifest. Verify its link count is one and retry the normal
   lifecycle operation so Forge validates the manifest and existing resources.

If identity, ownership, link count or expected worktree state is ambiguous,
preserve the files for investigation. There is no automatic orphan sweep: an
active publication can also temporarily have two links. A permanently failing
native handle close likewise requires stopping the affected process before
manual recovery; a timeout or error does not prove that handle was released.
