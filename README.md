# Parallel Forge

> **Status: active v0.1 development.** The durable backend and local execution
> foundations are under active construction; the agent workflow, dashboard,
> GitHub publication controller, and final operator experience remain roadmap work.

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
interfaces. The planned Next.js dashboard and deterministic Release Controller
are architectural targets, not completed user-facing features.

## Development status and roadmap

Tasks 1-12 of the v0.1 plan are complete and independently reviewed. Task 13 has
delivered controlled Git, isolated worktrees and databases, protected secrets,
durable resource preparation, environment staging, and worktree-bound runners;
durable ordered setup orchestration and lifecycle completion remain in progress.

Later roadmap stages add controlled agent tools and contracts, planning and
delivery workflows, REST/SSE projections, the dashboard, GitHub inspection,
human-approved PR publication and merge control, evaluation, restart recovery,
cross-platform CI, and final acceptance testing.

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

The final one-command development environment and dashboard are not available
yet.

## Documentation

- [Architecture](docs/architecture.md)
- [Threat model](docs/threat-model.md)
- [Full v0.1 design](docs/superpowers/specs/2026-08-21-forge-v0-1-design.md)
- [Implementation roadmap](docs/superpowers/plans/2026-08-21-forge-v0-1.md)

## License

Copyright 2026 Clar17y. Licensed under the Apache License, Version 2.0.
