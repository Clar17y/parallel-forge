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

---

# Task 11 Slice 2 worker report

## Status

Complete for the bounded repository process boundary. Repository listing,
file reading/search, and instruction discovery remain out of scope.

## TDD evidence

- RED: the new process test module failed collection with the expected
  `ModuleNotFoundError: No module named 'forge.tools.process'`.
- GREEN: `apps/orchestrator/tests/tools/test_process.py` — `20 passed`.
  The tests exercise real child processes for argv validation, shell
  metacharacters, exact cwd, explicit environment isolation, independent
  stdout/stderr bounds, invalid UTF-8, timeout/reap, nonzero exit, spawn
  secrecy, cwd containment/links/root replacement, and positive bounds.
- Combined focused path/process/Git-inspector suite — `49 passed, 2 skipped`.

## Delivered

- Added concrete `forge.tools.process.ProcessRunner` with explicit immutable
  argv/environment validation, no shell, `stdin=DEVNULL`, canonical contained
  cwd, positive timeout and per-stream byte-bound configuration, and generic
  `ProcessExecutionError` translation.
- Added concurrent bounded pipe drainers. Each stream retains at most its own
  configured byte limit while counting all original bytes and reporting an
  independent truncation flag. Output is decoded as UTF-8 with replacement.
- Added timeout kill/reap handling and bounded nonzero results. Spawn and pipe
  failures expose only the generic process error message.
- Extended `CanonicalRoot` with held no-follow directory capabilities for
  process launch. POSIX uses descriptor-relative directory traversal and a
  `/proc/self/fd/<descriptor>` cwd when available; Windows holds checked
  no-reparse handles with delete sharing denied. Root identity is revalidated
  around launch.

## Material decisions

- `ProcessRunner` requires a `CanonicalRoot`; `cwd` accepts the root (`.`), a
  contained normalized relative directory, or an absolute path that is an
  exact/descendant root path. Lexical parent/dot and outside paths fail as
  `PathEscape`; links/reparses fail as `RepositoryAccessDenied`.
- The process receives exactly the supplied environment mapping. No caller
  environment is copied, and NUL/empty argv or environment entries are
  rejected before spawn.
- Timeout returns a bounded `ProcessResult` with `timed_out=True` after
  terminating and reaping the child; ordinary nonzero exits remain results,
  not exceptions.

## Verification

- Focused process/path/security/Git pytest: `49 passed, 2 skipped`.
- Affected Task 8 artifact and Task 10 Git-inspector pytest:
  `20 passed, 2 skipped`.
- Full orchestrator pytest: `910 passed, 3 skipped in 100.07s`.
- Full Ruff check: pass (`All checks passed!`).
- Full Ruff format check: `139 files already formatted`.
- Full Forge mypy: `Success: no issues found in 96 source files`.
- Staged `git diff --check`: pass.

## Unresolved concerns

- POSIX platforms without `/proc/self/fd` retain the held descriptor through
  launch but must pass the canonical path to `subprocess`; the root is still
  revalidated before and after the launch boundary.
- Windows final process cwd link coverage follows the existing host capability
  policy for symlink/junction creation; no repository operations were added.

---

# Task 11 Slice 3a worker report

## Status

Complete for bounded repository listing and file reading. Search and
instruction discovery remain intentionally out of scope for this slice.

## TDD evidence

- RED: the initial `test_repository.py` collection failed with
  `ModuleNotFoundError: No module named 'forge.tools.repository'`.
- GREEN: the repository tests passed after the adapter was added; the later
  virtual-environment direct-read regression first failed, then passed after
  ancestor detection was added.

## Delivered

- Added `RepositoryReader` with a pinned `CanonicalRoot`, positive
  `max_file_bytes` and `max_list_entries` validation, configured secret,
  managed-worktree, and artifact exclusions, and fixed component exclusions.
- Added deterministic forward-slash regular-file listing with truthful byte
  sizes, fail-closed traversal bounds, exact-or-descendant exclusions, and
  exclusion of `.git`, worktrees, dependency environments, and directories
  containing a regular `pyvenv.cfg`. `.env.example` remains visible when
  `.env` is configured as a secret.
- Added bounded no-follow UTF-8 reads. Reads reject binary NUL and invalid
  UTF-8, preserve exact text including line endings, truncate only at a valid
  multibyte boundary, and retain original byte size/truncation metadata.
- Extended `CanonicalRoot` with descriptor/handle-held directory enumeration;
  POSIX uses descriptor-relative no-follow stats and Windows uses the existing
  no-reparse, delete-sharing-safe directory handles. Links/reparse entries are
  omitted from listings and direct access fails closed. Root identity is
  revalidated around enumeration and reads.
- Added coverage for bounds, exclusions, `.env.example`, virtual environments,
  binary/invalid UTF-8, multibyte truncation, linked entries, and root
  replacement.

## Material decisions

- `list_files` returns only regular files. Because the existing synchronous
  protocol exposes a sequence of `RepositoryEntry` records and no list-level
  truncation record, exceeding `max_list_entries` fails closed with the
  bounded generic `RepositoryAccessDenied` rather than returning an ambiguous
  partial result.
- A direct read below a directory containing `pyvenv.cfg` checks each existing
  ancestor through the safe directory primitive before opening file bytes.
  A symlink/reparse named `pyvenv.cfg` is not followed or treated as a virtual
  environment marker.

## Verification

- Task 11 path/process/repository/security focused pytest:
  `62 passed, 2 skipped in 1.74s`.
- Affected artifact and Git inspector pytest:
  `20 passed, 2 skipped in 0.70s`.
- Full orchestrator pytest:
  `923 passed, 3 skipped in 97.12s`.
- Full orchestrator Ruff check: `All checks passed!`.
- Full orchestrator Ruff format check: `141 files already formatted`.
- Forge mypy: `Success: no issues found in 97 source files`.
- `git diff --check`: pass before staging; staged check was repeated before
  commit.

## Unresolved concerns

- Final-file symlink coverage remains capability-dependent on this Windows
  host, as documented by the earlier path-boundary slice; directory junction,
  intermediate-link, linked-entry, and root-replacement cases execute here.
- No search or instruction-discovery behavior was added in this slice.

---

# Task 11 Slice 3b1 worker report

## Status

Complete for the bounded pure-Python literal-search backend only. Ripgrep
integration and instruction discovery remain intentionally out of scope for
this micro-slice.

## TDD evidence

- RED: the new search tests collected but failed because the reader lacked the
  search bounds and `search` method.
- GREEN: `test_repository.py` passed with 22 tests after the bounded fallback
  was implemented.

## Delivered

- Added nonempty strict UTF-8 literal validation, including rejection of NUL
  and lone-surrogate queries without echoing query content.
- Added deterministic forward-slash/path then line-order search results with
  one `SearchMatch` per matching line and a configurable positive global match
  cap (default 100).
- Added a positive inspected-candidate byte cap (default 8 MiB) that fails
  closed before reading a candidate that would exceed the remaining budget.
- Reused `read_file` bounded UTF-8/NUL behavior; binary and invalid UTF-8
  candidates are skipped consistently without exposing their contents.
- Omitted hidden path components, fixed/configured/secret/virtual-environment
  exclusions, and retained exact `.env.example` visibility/searchability.
- Accepted `rg_executable=None` as an explicit constructor setting while this
  micro-slice deliberately uses Python only.

## Material decisions

- Exceeding the search byte budget fails closed with bounded
  `RepositoryAccessDenied`, matching the existing list-bound behavior rather
  than returning an ambiguous partial search.
- `.env.example` is the deliberate hidden-file exception required by this
  slice; other dot-prefixed path components remain omitted.

## Verification

- Task 11 focused path/process/repository/security pytest:
  `66 passed, 1 skipped in 1.32s`.
- Repository Ruff check: `All checks passed!`.
- Repository Ruff format check: `2 files already formatted`.
- Forge repository mypy: `Success: no issues found in 1 source file`.
- `git diff --check`: pass.

## Unresolved concerns

- Ripgrep process invocation, JSON parsing, and instruction discovery are
  deferred to the next bounded slice; no subprocess/search fallback switching
  was added here.
