# Forge runner image contract

Configure the final runner image by immutable image ID or repository digest.
`Dockerfile.runner` supplies Python 3.14, Node 24, repository build tools and the
non-root user `10001:10001`. Repository commands remain exact policy-named argv
vectors, with one managed worktree mount, allowlisted environment, no Docker
socket and network disabled unless the named command explicitly allows it.

On Linux, the Docker daemon must expose the same kernel and filesystem identity
as the Forge worker. Forge retains the directory descriptor through execution
and passes its device/inode plus the host kernel boot ID to the fixed entrypoint
`/usr/local/bin/forge-mount-guard`. The guard starts at `/`, verifies the kernel
and mounted directory, changes directory through the verified descriptor, and
only then executes the registered command. Remote daemons and VM-backed Linux
contexts with different identities fail closed; Forge does not fall back to an
unchecked path. Windows uses the existing retained Windows directory handles,
including when Docker Desktop translates the bind mount into its Linux VM.

Custom Linux runner images must include the guard built from
`scripts/runner-mount-guard.c` at that exact path. It must be statically linked,
owned by the image builder and unavailable for repository writes. Forge overrides
the image entrypoint, so no image startup script runs ahead of the guard. Static
linking prevents `LD_PRELOAD` and other dynamic-loader settings from executing
repository code before the identity check. Missing guards, unavailable identity
information and mismatches fail without running the command. Image builders are
trusted; an immutable digest identifies their reviewed image and is not an
attestation that an arbitrary image implements this contract.

The guard uses the trusted image's `PATH` to resolve the registered executable.
Image builders must exclude empty and relative PATH entries; project policy
cannot override PATH. Rejection diagnostics contain only fixed stage names,
never repository paths, identity values or command arguments. Exit 126 alone is
not an authenticated boundary-failure signal: a repository command can also
return that code or print the same text.

On native Linux, Forge grants UID 10001 a descriptor-scoped ACL lease across
the managed worktree. After exact command-container termination, the guard
performs a bounded, descriptor-only repair of UID 10001 output
entries. Repaired files retain an exact named-user ACL for the host and UID
10001; Forge checks this access precondition before reading a container-owned
file under a Docker project capability. It is not provenance or authenticity:
the owning UID can create the same ACL itself.
The lease then restores pre-existing file and directory ACLs. Existing staged
environment files retain their exact read-only ACL. A failed
container cleanup or repair fails closed and leaves the managed worktree for
operator recovery.

Native Linux recovery requires a kernel implementing `fchmodat2` with
`AT_EMPTY_PATH` (Linux 6.6 or newer) and a Docker seccomp profile permitting it.
The static guard probes that operation in its private temporary directory before
executing repository code; unsupported hosts fail with the fixed diagnostic
`access-repair-unsupported`. Normal accessible entries do not need that syscall
during traversal. Recovery removes special set-ID/sticky bits from runner-owned
outputs and preserves executable permissions.

Symlinks are skipped without following their targets during ACL preparation and
repair. Generated output trees do not retain one host descriptor per file.
Traversal remains bounded to 100000 entries and 128 directory levels, with at most
4096 retained descriptors for entries whose original host ACLs need restoration.
Lower operating-system descriptor limits can reject a lease safely; Forge does
not raise host process limits or discard restoration identity to bypass them.
If an interrupted repair or an external chmod invalidates a runner-owned ACL,
Forge fails closed. Recovery may require a host administrator to restore access
to the retained resources; Forge does not silently accept a noncanonical ACL or
gain privileges for recovery.

The focused Linux Docker smoke runs independently of the backend suite:

```text
python -m pytest tests/integration/test_runner_container.py -q
```

It checks the image/tool versions, a real capability-bound staged-file command,
and guard rejection before command or malicious loader-constructor execution.

## Running Forge's Python checks on Docker Desktop

The self-development project can load its locked Python dependencies from the runner image. Forge and its tests still load from the mounted worktree, including uncommitted edits. This avoids reading the worktree's virtual environment across the Windows file share during each check.

Start the local supervisor with the optional image target.

```sh
python scripts/dev.py --forge-checks
```

The supervisor builds the `forge-checks` target and gives the worker that build's immutable image ID. For a separately started worker, use the ID printed by this command for `FORGE_RUNNER_IMAGE`.

```sh
docker build --quiet --platform linux/amd64 --target forge-checks -f Dockerfile.runner -t parallel-forge-runner:forge-checks .
```

For Docker-backed checks, replace the three leading arguments `.venv/bin/python -m pytest` with `forge-pytest` in a new project policy version. Keep the check's marker expression, remaining arguments and path scope. The Docker `test-backend` argument vector is

```json
["forge-pytest", "-m", "not integration and not docker and not live_provider and not live_github", "-q"]
```

For `test-backend-focused`, keep the existing `path_scope` so Forge appends the task's eligible test paths to this vector.

```json
["forge-pytest", "-q", "-p", "no:cacheprovider", "-m", "not integration and not docker and not live_provider and not live_github"]
```

If the project also uses direct runs, keep the unscoped `test-backend` command executable on the host. Direct runs execute it there, where an image-only `forge-pytest` command will fail. They skip path-scoped checks, so the focused Docker check can use `forge-pytest` in that shared policy.

The launcher checks the mounted `pyproject.toml` and `uv.lock` against the image's build inputs before starting pytest. Rebuild the image when those inputs change. Operators can also select the worktree's `.venv/bin/python -m pytest` in the policy. The receipt links to the command result, which records `duration_ms` and the immutable image ID. For version 3 receipts, the named-check outcome exposes the same duration as `command_duration_ms`.

### Measured on Windows Docker Desktop

On 2026-11-21 both images ran the self-development policy's offline selection on the same read-only checkout at `51f8f0c`. Each container used Python 3.14.2, pytest 8.4.2, two CPUs, 2 GiB of memory and no network. Wall time includes container startup and exit. The worktree environment used image `f20c73552d15` and the image-based environment used `42b60da45c9f`.

| Check | Worktree virtualenv | Image dependencies | Result on both |
| --- | ---: | ---: | --- |
| Domain tests, median of three runs | 19.1 s | 10.5 s | 443 passed |
| API tests, one run each | 106.4 s | 70.0 s | 339 passed, 23 deselected |
| Full offline collection, one run each | 74.7 s | 49.6 s | 6,376 selected, 3,232 deselected |

The collected cases matched after accounting for one test ID containing a generated UUID. Two state-transition tests also varied their parameter order between processes. These runs used serial pytest.

On the implementation worktree based on `a660e67`, a separate run of `test-backend-focused` through Forge's Docker runner passed all 561 domain tests. Its command result recorded 12,074 ms.

The full offline `test-backend` command at `4b5253b` took 443.8 s with image dependencies. Pytest reported 6,445 passed, 124 skipped, 11 failed and 3,497 deselected. All 11 failing cases also failed with the worktree virtualenv and the original runner image on both `4b5253b` and its parent `a660e67`. The full suite has a single timing for the image-based environment.
