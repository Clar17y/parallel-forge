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

The focused Linux Docker smoke runs independently of the backend suite:

```text
python -m pytest tests/integration/test_runner_container.py -q
```

It checks the image/tool versions, a real capability-bound staged-file command,
and guard rejection before command or malicious loader-constructor execution.
