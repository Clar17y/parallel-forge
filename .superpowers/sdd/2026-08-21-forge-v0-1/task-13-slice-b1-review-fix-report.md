# Task 13 slice B1 review-fix report

- Base candidate: `54dbe5ade6747e06731a745f1b86d84054574843`.
- Scope: accepted B1 review fixes only; no B2 worktree mutation or commit
  behavior was added.

## Regression-first evidence

- Red: the new regression run failed with 9 expected failures: seven forged
  worktree-name cases, caller-selected ancestry ref acceptance, and optional
  Git executable discovery.
- Focused green:
  `\.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/tools/test_git.py apps/orchestrator/tests/domain/test_resource.py -q`
  — 30 passed, 1 skipped.
- Affected green:
  `\.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/tools/test_git.py apps/orchestrator/tests/domain/test_resource.py apps/orchestrator/tests/tools/test_process.py apps/orchestrator/tests/tools/test_paths.py apps/orchestrator/tests/application/test_repository_inspection.py -q`
  — 76 passed, 3 skipped.

## Fixes

- `WorktreeIdentity` now accepts only one lowercase ASCII filename component
  matching the generated `alphanumeric-hyphen` form; absolute, nested,
  backslash, drive-like, dot, and traversal names fail at the domain boundary.
- `ControlledGitPort.is_ancestor` and `ControlledGit.is_ancestor` now accept
  only the managed handle and always use its recorded `base_sha`.
- `ControlledGit` now requires an operator-supplied absolute `git_executable`;
  PATH discovery was removed. Existing focused fixtures pass the trusted
  absolute executable explicitly, and a rogue PATH-front executable regression
  confirms it is never selected.

## Static and hygiene evidence

- Ruff check passed for touched source and tests.
- Ruff format check passed for touched source and tests.
- Mypy passed for `apps/orchestrator/src/forge` — 105 source files.
- `git diff --check` passed.

## Unresolved concerns

- None within the accepted B1 review-fix scope.
