# Parallel Forge

> **Status: active v0.1 development.** The durable backend and local execution
> foundations, agent workflow, dashboard and Release Controller have substantial
> implementation and component coverage. Process/browser acceptance and independent reviews have passed;
> final integrated backend CI remains pending; see the [evidence ledger](docs/v0.1-progress.md).

Parallel Forge is a local-first control plane for durable, reviewable
agent-assisted software delivery. It began as the engineering system for
building Parallel, but is designed to manage other repositories independently.

## What exists today

- PostgreSQL-backed workflow state, durable commands, leases, operation intents,
  causal events, telemetry, redaction, and usage accounting
- content-addressed artifact storage and lineage
- local operator authentication and evidence-bound approval primitives
- confined repository reading and controlled Git/worktree/commit operations
- protected local secrets, isolated PostgreSQL resources, and environment staging
- Docker-first and explicit trusted-host command execution bound to exact managed
  worktrees

## Safety model

Model-driven agents never receive push, pull-request write, merge, approval,
credential, policy-write, or Forge-database authority. Remote writes are reserved
for a deterministic Release Controller and require exact human-approved evidence.

## Architecture

The FastAPI control API and separate orchestrator worker communicate through
PostgreSQL-backed commands, leases, state, events, and operation intents. A local
content-addressed store retains bounded evidence, while Forge-owned adapters bind
repository, Git, worktree, database, secret, and runner effects to controlled
interfaces. The Next.js dashboard exposes evidence and state-aware controls;
the deterministic Release Controller owns approved GitHub effects. Candidate-specific acceptance results are recorded in the evidence ledger.

## Local development

Use Python 3.14, Node.js 24, uv, npm, Git and Docker, with the repository's
PostgreSQL compose service healthy. Configure the control database, data root,
provider secret reference and pricing catalog as described in the
[operator runbook](docs/operator-runbook.md).

Run `npm run dev` (or `scripts/dev.ps1` / `bash scripts/dev.sh`) to install the
frozen dependencies, migrate, build the runner and supervise the API, worker and
web app. This command rotates the operator session and prints a fresh bootstrap
URL. Hosted startup, recovery and shutdown acceptance has passed.

Use `npm run test`, `npm run lint`, `npm run typecheck` and `npm run build` for
focused development checks. `npm run verify` includes the deterministic backend
and web checks; PostgreSQL and Docker must be available. Browser acceptance is a
separate `npm run test:e2e` command covering the approval flow, restart/cancellation
and keyboard/accessibility behavior.

## Development status and roadmap

Tasks 1–29 are implemented, including release/queue recovery, dashboard/resource
controls, evaluations, process acceptance and cross-platform CI. Independent
review findings are closed. Final integrated backend verification remains pending.
The [progress ledger](docs/v0.1-progress.md) distinguishes current evidence from
historical test results and is the authoritative continuation record.

## Prerequisites and verification

- Python 3.14
- Node.js 24
- PostgreSQL 17
- Docker
- `uv sync --frozen --extra dev`
- `docker compose up -d postgres`
- `.venv/Scripts/python.exe -m pytest -q` on Windows or
  `.venv/bin/python -m pytest -q` on POSIX
- `.venv/Scripts/python.exe -m ruff check apps/orchestrator` and
  `.venv/Scripts/python.exe -m mypy apps/orchestrator/src` on Windows, with the
  equivalent `.venv/bin/python` commands on POSIX

The one-command development supervisor has passed hosted startup acceptance.
Manual process startup, configuration, approvals and recovery are described in the
[operator runbook](docs/operator-runbook.md).

## Documentation

- [Operator runbook](docs/operator-runbook.md)
- [Verified progress](docs/v0.1-progress.md)
- [Architecture](docs/architecture.md)
- [Threat model](docs/threat-model.md)
- [Full v0.1 design](docs/superpowers/specs/2026-08-21-forge-v0-1-design.md)
- [Implementation roadmap](docs/superpowers/plans/2026-08-21-forge-v0-1.md)

## License

Copyright 2026 Clar17y. Licensed under the Apache License, Version 2.0.
