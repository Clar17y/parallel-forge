# Forge repository guidance

## Runtime and boundaries

- Use Python 3.14 only and Node.js 24.x only.
- PostgreSQL is the system of record. Do not add SQLite, Redis, Celery, or an
  in-memory production fallback.
- The API and worker are separate processes and communicate through durable
  PostgreSQL state; do not make the worker an API subcommand.
- Keep role boundaries independent: the API serves requests, the worker runs
  durable work, and the CLI is an operator entry point.
- Keep Google ADK and provider details behind Forge-owned interfaces. Agent
  roles receive only named, controlled tools.
- Repository writes belong only inside an explicitly managed worktree.
- Human approval gates are explicit and evidence-bound. Never merge a pull
  request without immediate, explicit human authorization.

## Development workflow

- Follow test-driven development: write one focused failing test, run it and
  observe the expected failure, implement the smallest behavior, rerun the
  focused test, then run the affected suite.
- Use `python -m pytest ... -q` for focused Python checks.
- Use `rtk` for noisy commands such as lint, type checking, and full test runs.
- Keep credentials out of source, logs, artifacts, and persisted run records.
- Preserve unrelated work and do not rewrite or reset another agent's changes.

## Controlled command runner

- Repository commands are selected only by exact names from the active,
  versioned project policy. Forge agents never supply shell text, argv, mounts,
  images, environment keys, network settings, or Docker flags.
- Docker is the default. The runner mounts only the canonical managed worktree,
  runs as UID/GID 10001, receives only allowlisted environment values, has no
  Docker socket, and defaults to no network. Trusted-host mode is explicitly
  unsandboxed and is valid only for an operator-designated trusted project.
- The linux/amd64 Python runner base is pinned to
  `python:3.14.2-slim@sha256:51f5baff157fee39a31e5b32394dde7ed2977bcea7a0b16a8978a8d23c270f85`.
  The Node extraction stage is pinned to
  `node:24.19.0-slim@sha256:65932751ed4073ed02f5c04e494e4b2572a891b7dbea0568a863dc80341bf848`.
  Any configured final runner image must also be addressed by its immutable
  `sha256:` image ID or repository digest; mutable tags are rejected.
- Command output is untrusted, bounded, redacted, and persisted only as
  evidence artifacts. Never log command environment values or put them in the
  Docker argv.
