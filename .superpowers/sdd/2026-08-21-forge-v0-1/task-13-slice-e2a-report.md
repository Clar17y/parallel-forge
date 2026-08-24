# Task 13 Slice E2a report

## Scope

E2a only: policy-controlled environment path validation, database environment
rematerialization, opaque worktree capabilities, protected environment staging,
native path safety, and digest-only evidence. E2b container readability and E2c
setup/orchestration remain deferred.

## Review repairs and security mechanics

- Policy paths use one canonical validator, ordered effective-secret-path union,
  exact reserved-component rejection, Windows ADS/device/trailing-dot-space
  rejection, cross-field case-alias rejection, and no-follow bounded reads.
- `WorktreeCapability` and `EnvironmentStagingPlan` are owner-sealed. A public
  plan contains only an opaque token and digest-only evidence; raw paths, source
  bytes, and output bytes remain in an `EnvironmentStager`-owned record. Exact
  stager, worktree identity, policy identity/version, token identity, and all
  path/source/output digests are checked immediately before publication.
  Copied, foreign, forged, or reflectively mutated plans fail closed. This is an
  API capability boundary, not a claim that Python reflection defeats a hostile
  process with memory access.
- Publication inspects the complete destination set before any write. Only a
  totally absent set publishes; an exact complete set is idempotent; partial,
  unsafe, or different existing destinations raise reconciliation without
  changing the destination set.
- Publication is bounded, exclusive, single-link, permission-checked,
  flush/fsync verified, reopened, content-verified, and cleans exact temporary
  names on injected write/link/flush/reopen/cleanup faults.
- Windows uses native handle-relative no-follow opens, protected DACL checks,
  explicit sharing, and reparse rejection. Linux Docker ACLs use native
  dynamically loaded `libacl` and require the exact UID-10001 read entry;
  missing, unsupported, application, or verification failures fail closed.
  Linux owner checks require the current POSIX UID.
- Inspection requests existing-only mutation-lock access, so a missing Forge
  lock cannot create lock or registration metadata.
- Database ACTIVE rematerialization reuses authoritative read-only proof without
  operation-row mutation; disabled rematerialization rejects any nonempty
  environment before touching dependencies. Secret and URL values are absent
  from representations, evidence, and sanitized exception chains.

## Verification

Windows focused and affected evidence:

- `test_environment_staging.py`: `52 passed, 6 skipped` in 43.82 seconds.
- `test_process.py`: `23 passed, 1 skipped` in 1.82 seconds.
- Affected E2a suites (staging, process, paths, Git, database provisioner,
  policy, repository containment/inspection, secret-store, redaction, and
  telemetry): `503 passed, 50 skipped` in 226.27 seconds.
- Candidate-scoped Ruff check: passed (`All checks passed!`).
- Candidate-scoped Ruff format check: passed (`9 files already formatted`).
- Mypy over `apps/orchestrator/src`: passed, no issues in 110 source files.
- Full configured Windows pytest suite: `1429 passed, 53 skipped` in 339.38
  seconds.

WSL native proof used the disposable `/tmp/forge-e2a-venv` uv environment
(Python 3.14.0rc3; `libacl.so.1` available), with `-p no:cacheprovider`; no
project lock or dependency files were modified:

```text
wsl.exe -d Ubuntu -- bash -lc 'cd "/mnt/d/Code/Parallel Forge/.worktrees/forge-v0-1" && PYTHONPATH=.:apps/orchestrator/src /tmp/forge-e2a-venv/bin/pytest -p no:cacheprovider -q apps/orchestrator/tests/tools/test_environment_staging.py'
51 passed, 7 skipped in 4.52s

... test_environment_staging.py -k "linux_acl or linux_docker"
4 passed, 54 deselected in 1.35s

... test_process.py -k posix_launch_inherits
1 passed, 23 deselected in 0.51s
```

The real Linux staging test published a Docker-mode file on a real filesystem,
then inspected the native ACL text and found `user:10001:r--`. The Linux
unavailable, ACL application-failure, and ACL verification-failure cases also
passed. The descriptor regression verified that every `/proc/self/fd/N`
referenced by the POSIX launch is inherited.

## Root causes corrected

The earlier Windows RED first reported temporary cleanup, but the underlying
failure was the final `local.env` reopen using an incompatible native access /
share contract (`0x80020000`, share `0` mapped to WinError 87). The reopen
contract was corrected and exact temporary disposal then passed.

The initial WSL RED was a real launch-binding bug: the POSIX no-registration
branch omitted the target capability fd from `pass_fds`, while the launch cwd
used `/proc/self/fd/N`; the child reported that the cwd did not exist. The
target fd is now always inherited, managed launches also inherit registration
and Git fds, and `Popen` uses a parent-process fd path so descriptor resolution
occurs after inheritance.

## Platform and deferred limits

Windows-native DACL/reparse/share and fault paths are platform-gated where
appropriate; the WSL run supplies the real Linux/libacl proof. No container was
started and no fixed-UID container-read claim is made; that gate belongs solely
to E2b. E2c setup, transitions, teardown, migrations, CLI, push, PR, and merge
remain outside E2a.
