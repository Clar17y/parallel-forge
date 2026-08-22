# Task 13 slice B1 report

- Base: `e754cbeeb647d62909c0ad4dbfb11789730b56ec`.
- Scope: immutable managed-worktree handles, bounded Git output types, trusted
  state-root setup, isolated ProcessRunner-backed Git invocation, local-config
  safety scan, and handle-bound read operations only. No worktree creation,
  removal, commit, branch deletion, or arbitrary command surface was added.
- Candidate commit SHA: recorded in the final handoff; the report avoids a
  self-referential commit hash.

## TDD evidence

- Red: the first focused collection run failed with the expected missing
  `forge.application.ports.worktrees` import.
- Green: `\.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/tools/test_git.py -q`
  — 9 passed, 1 skipped.
- Affected green:
  `\.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/tools/test_git.py apps/orchestrator/tests/tools/test_process.py apps/orchestrator/tests/tools/test_paths.py apps/orchestrator/tests/application/test_repository_inspection.py -q`
  — 55 passed, 3 skipped.

## Static and hygiene evidence

- Ruff check passed for the two new source modules and focused tests.
- Ruff format check passed for the two new source modules and focused tests.
- Mypy passed for `apps/orchestrator/src/forge` — 105 source files.
- `rtk git diff --check` passed.

## Material implementation choices

- `ManagedWorktree` is frozen and slot-based; it normalizes to a canonical
  absolute `Path` and accepts only a lowercase 40-hex base SHA.
- `ControlledGit` binds one `CanonicalRoot`, the exact `<root>/.worktrees`
  namespace, one validated default branch, an absolute regular Git executable,
  and a state root outside the repository. The state root owns an empty hooks
  directory, global config, and global attributes file and is revalidated on
  every call.
- Git invocations use `shell=False` through `ProcessRunner`, exact absolute
  `-C` and cwd values, `--no-pager`, Forge-owned `-c` isolation for hooks,
  signing, credentials, fsmonitor/untracked cache, external diff, attributes,
  and author identity, plus a narrow sanitized environment.
- Status and diff run a local key-only config scan with includes disabled and
  refuse hooks, filters, fsmonitor, external diff/textconv, credentials,
  pager/editor, SSH/proxy, attributes, and include settings before the
  requested operation. Worktree handles are checked for exact path,
  no-link/non-reparse components, registration metadata, and recorded branch.
- Identity operations reject timeout, malformed, non-lowercase, or truncated
  output. Status/diff results retain bounded output and truthful truncation and
  byte-count metadata.

## Unresolved concerns

- None within the fixed B1 scope. Worktree mutation and commit operations remain
  intentionally reserved for the subsequent slices.
