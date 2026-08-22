# Task 11 Slice 1 worker report

## Status

Complete for the canonical confined path boundary only. Repository listing,
file reading/search, instruction discovery, and process execution remain out of
scope for this slice.

## TDD evidence

- Existing candidate RED/GREEN evidence was preserved. The initial focused
  path/security command collected the candidate and passed its 18 tests with
  4 capability skips.
- After making the link fixtures exercise Windows junctions and entering the
  `open_read` context managers, the focused suite produced the intended RED:
  `2 failed, 20 passed, 1 skipped`. The failures were the real intermediate
  junction accesses that had previously been hidden by non-entered context
  managers.
- GREEN after the boundary repair and adversarial coverage:
  `apps/orchestrator/tests/tools/test_paths.py`
  plus `apps/orchestrator/tests/security/test_repository_escape.py` —
  `22 passed, 1 skipped`.

## Delivered

- Added immutable synchronous repository result/error/protocol foundations in
  `application/ports/repository.py`, including the distinct `PathEscape`
  subtype and always-true untrusted instruction marker.
- Added `CanonicalRoot` with absolute-root validation, component-by-component
  symlink/reparse rejection, pinned filesystem identity, strict relative path
  normalization, path-boundary matching, regular-file stat, and no-follow
  contained reads.
- POSIX reads use descriptor-relative `O_NOFOLLOW` traversal, descriptor
  identity checks, duplicated stream ownership, and root revalidation around
  access. Windows reads use held no-delete-sharing handles, explicit
  `FILE_ATTRIBUTE_REPARSE_POINT` checks for every traversed component, final
  regular-file checks, identity checks, and root revalidation.
- Added Windows junction-capable fixtures so directory reparse tests skip only
  when neither symlink nor junction creation is available. Added coverage for
  absolute/UNC/drive/alternate-separator/dot/parent paths, sibling prefixes,
  exact-or-descendant secret matching (`.env` vs `.env.example`), root and
  intermediate/final links, root replacement, contained reads, and Windows
  case-insensitive matching.

## Material decisions

- Relative caller strings must use forward slashes; native Windows `Path`
  objects remain accepted because their parsed components are already safe and
  are normalized to forward-slash output. Matcher comparisons case-fold on
  Windows without changing display-path spelling.
- Directory reparse entries are opened with `OPEN_REPARSE_POINT` and rejected
  from handle metadata before any descendant is opened. Held handles deny
  delete sharing, preventing a validate-then-open replacement race.
- POSIX stream ownership is transferred through `os.dup` so the descriptor
  context never closes a recycled file descriptor after `fdopen` closes it.

## Verification

- Focused path/security pytest: `22 passed, 1 skipped`.
- Affected Task 8 artifact and Task 10 Git-inspector tests:
  `20 passed, 2 skipped`.
- Full orchestrator pytest: `890 passed, 3 skipped in 95.15s`.
- Full source/test Ruff check: pass (`All checks passed!`).
- Full source/test Ruff format check: `137 files already formatted`.
- Full Forge mypy: `Success: no issues found in 95 source files`.
- Staged `git diff --check`: pass.

## Unresolved concerns

- The final-file reparse test is skipped on this Windows host because it
  permits junctions but not unprivileged file symlink creation; directory
  junction/root/intermediate coverage runs. A host with file-link capability
  executes the final-link test.
- Later repository operations must bound bytes and apply the same canonical
  primitives; this slice intentionally does not add those adapters.

## Candidate

Candidate commit: `feat: add canonical repository path boundary` (the
committed candidate SHA is returned with this report).
