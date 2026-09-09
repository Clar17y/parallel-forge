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
