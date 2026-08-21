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
