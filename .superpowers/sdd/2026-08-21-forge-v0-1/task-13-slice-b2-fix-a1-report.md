# Task 13 B2 fix A prerequisite report

- Base: `b040641072b917521d5408009e91caff309906ab`.
- Scope: Windows directory capability access and its focused regression only.
  No `git.py`, `test_git.py`, removal, B3, database, lifecycle, or CLI work was
  changed.
- Candidate commit SHA: recorded in the final handoff; this report avoids a
  self-referential commit hash.

## TDD evidence

- Red: `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/tools/test_paths.py -q -k open_directory_blocks_rename_until_capability_is_released`
  — 1 failed, 20 deselected. The baseline failure was the expected
  `DID NOT RAISE <class 'PermissionError'>` while the held directory was
  renamed.
- Green: the same focused command — 1 passed, 20 deselected.
- Affected green:
  `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/tools/test_paths.py apps/orchestrator/tests/tools/test_process.py -q`
  — 40 passed, 1 skipped.

## Static and hygiene evidence

- `.venv\\Scripts\\python.exe -m ruff check apps/orchestrator/src/forge/tools/paths.py apps/orchestrator/tests/tools/test_paths.py`
  — passed.
- `.venv\\Scripts\\python.exe -m ruff format --check apps/orchestrator/src/forge/tools/paths.py apps/orchestrator/tests/tools/test_paths.py`
  — 2 files already formatted.
- `.venv\\Scripts\\python.exe -m mypy apps/orchestrator/src/forge`
  — no issues found in 105 source files.
- `git diff --check` — passed.

## Material implementation choices

- Replaced the Windows directory handle's desired access
  `FILE_READ_ATTRIBUTES` with the minimal `FILE_LIST_DIRECTORY` bit
  (`0x00000001`). Existing read/write sharing remains, and delete sharing is
  still omitted; reparse-point and directory checks are unchanged.
- Added a Windows-only real-filesystem regression that holds
  `CanonicalRoot.open_directory("src")`, proves a rename is denied while the
  capability is held, and proves the rename succeeds after release.
- POSIX code and public APIs are unchanged.

## Unresolved concerns

- None within this prerequisite. The POSIX branch was not runtime-exercised on
  this Windows host; it is unchanged and remains covered by non-Windows test
  execution.
