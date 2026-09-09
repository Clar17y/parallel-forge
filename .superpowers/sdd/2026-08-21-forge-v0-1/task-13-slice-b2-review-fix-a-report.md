# Task 13 slice B2 review fix A report

- Base: `1469570ee681f191f7f589b0ed96c08c553a07ac`.
- Candidate commit SHA: recorded in the final handoff; this report avoids a
  self-referential commit hash.
- Scope: exact-target Git worktree-registration discovery and public invalid
  branch error normalization. Creation capabilities, removal redesign, B3,
  database, lifecycle, and CLI remain out of scope.

## TDD evidence

- Baseline Git suite before the fix: 30 passed, 1 skipped.
- RED: the metadata-basename regression failed against the basename-indexed
  lookup (2 failed). The invalid `bad..branch` regression exposed the raw
  `ValueError` from the public create boundary.
- GREEN focused A2 regressions:
  `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/tools/test_git.py -q -k
  "metadata_basename or unrelated_valid or duplicate_exact or
  malformed_registration or linked_registration or registration_entry_cap or
  create_refuses_default_or_invalid or create_preflight_matches or
  remove_uses_exact_target"`
  — 15 passed, 1 skipped, 28 deselected.
- GREEN full Git tests:
  `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/tools/test_git.py -q -rs`
  — 42 passed, 2 skipped. Both skips are host symlink-privilege skips.
- Affected path/process tests:
  `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/tools/test_paths.py
  apps/orchestrator/tests/tools/test_process.py -q`
  — 40 passed, 1 skipped.

## Static and hygiene evidence

- `.venv\\Scripts\\python.exe -m ruff check apps/orchestrator/src/forge/tools/git.py
  apps/orchestrator/tests/tools/test_git.py` — passed.
- `.venv\\Scripts\\python.exe -m ruff format --check
  apps/orchestrator/src/forge/tools/git.py apps/orchestrator/tests/tools/test_git.py`
  — 2 files already formatted.
- `.venv\\Scripts\\python.exe -m mypy apps/orchestrator/src/forge` — no issues
  found in 105 source files.
- `git diff --check` — passed.

## Material implementation choices

- Replaced metadata-basename prediction with a bounded scan of
  `.git/worktrees` (256 entries). Every candidate is checked as a no-link
  directory with a regular no-link `gitdir` file, a UTF-8 record no larger than
  the existing 4096-byte bound, and exactly one terminating line. The record
  is resolved without accepting existing links and compared to the exact
  `<repository>/.worktrees/<identity.worktree_name>/.git` target.
- Unrelated valid registrations are ignored; duplicate exact-target matches,
  malformed/link/reparse candidates, and cap overflow fail closed. The same
  finder now serves create preflight, handle validation, and the existing
  removal lookup.
- `create_worktree` (and handle-shape validation) catches branch-validation
  `TypeError`/`ValueError` and exposes only `ControlledGitError`; branch grammar
  and constructor validation are unchanged.
- The existing `git worktree remove --force -- <exact path>` deletion algorithm
  is unchanged.

## Unresolved concerns

- Symlink/reparse regressions were skipped where this Windows host lacks
  symlink creation privilege; the existing no-link/reparse checks are unchanged
  and the new scan uses them for both directory and `gitdir` candidates.
