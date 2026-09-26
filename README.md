# Parallel Forge

> **Status: v0.2 functional acceptance complete for personal operator use.**
> A1–A9 and the remaining filesystem checks have recorded outcomes. See the
> [release handoff](docs/v0.2-release.md) for supported behavior, evidence,
> platform limitations and the final delivery gates. Historical full-suite
> failures remain explicit; focused acceptance is not a new full-CI pass.

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
- subscription-backed local CLI orchestration, bounded specialist delegation,
  shared-worktree ownership and independent-worktree concurrency
- frozen routing profiles, durable quota/fallback state, per-task controls,
  operator feedback and measured/unknown usage reporting

## Safety model

Forge-controlled agent tools grant no push, pull-request write, merge, approval,
credential, policy-write, or Forge-database authority. Forge's remote writes are
reserved for a deterministic Release Controller and require exact human-approved evidence.
Operator-trusted clients may expose their own native tools; the
[accepted provider policy](docs/v0.2-release.md#accepted-provider-policy) describes
that limitation and the controls Forge continues to enforce.

## Architecture

The FastAPI control API and separate orchestrator worker communicate through
PostgreSQL-backed commands, leases, state, events, and operation intents. A local
content-addressed store retains bounded evidence, while Forge-owned adapters bind
repository, Git, worktree, database, secret, and runner effects to controlled
interfaces. The Next.js dashboard exposes evidence and state-aware controls;
the deterministic Release Controller owns approved GitHub effects. Candidate-specific acceptance results are recorded in the evidence ledger.

## Local development

Use Python 3.14, Node.js 24, uv, npm, Git and Docker, with the repository's
PostgreSQL compose service healthy. Configure the control database and data root
as described in the [operator runbook](docs/operator-runbook.md). For subscription
work, use the [local CLI setup guide](docs/v0.2-profile-cli.md) and an installation
manifest pointing to existing official-client logins. The default is operator
trust; provider API keys and capability publication are not startup prerequisites.
Antigravity retains its `approved_tools_unproved` warning.

Run `npm run dev` (or `scripts/dev.ps1` / `bash scripts/dev.sh`) to install the
frozen dependencies, migrate, build the runner and supervise the API, worker and
web app. This command rotates the operator session and prints a fresh bootstrap
URL. The operator runbook records retained supervisor startup/shutdown evidence.

Use `npm run test`, `npm run lint`, `npm run typecheck` and `npm run build` for
development checks. `npm run verify` includes the deterministic backend
and web checks; PostgreSQL and Docker must be available. Browser acceptance is a
separate `npm run test:e2e` command covering the approval flow, restart/cancellation
and keyboard/accessibility behavior.

For a reproducible, evidence-recording local run, use
`npm run verify:local -- list` and then
`npm run verify:local -- run <selection> [--focused TARGET ...]`. That harness
runs one explicit selection at a time, uses frozen dependencies and an ephemeral
isolated PostgreSQL instance for the Linux selections, makes no live provider call,
and records candidate identity, exact commands, outcomes and owned-container
cleanup under `.llm-output/local-verification/`. See
[reproducible local verification](docs/local-verification.md).

## Development status and roadmap

The [v0.2 release handoff](docs/v0.2-release.md) combines the completed functional
work, current setup and stopped-upgrade guidance. The
[v0.2 progress ledger](docs/v0.2-progress.md) distinguishes current outcomes from
historical checkpoints. The [remaining issue queue](docs/v0.2-issue-backlog.md)
tracks final publication and merge; one-run profile overrides are post-v0.2.

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

Manual process startup, configuration, approvals and recovery are described in the
[operator runbook](docs/operator-runbook.md).

## Documentation

- [Operator runbook](docs/operator-runbook.md)
- [v0.2 release handoff](docs/v0.2-release.md)
- [Local CLI profiles and readiness](docs/v0.2-profile-cli.md)
- [Subscription task controls](docs/v0.2-task-inspector.md)
- [Stopped upgrade and recovery](docs/v0.2-upgrade.md)
- [v0.2 progress](docs/v0.2-progress.md)
- [Historical v0.1 acceptance](docs/acceptance-v0.1.md)
- [Architecture](docs/architecture.md)
- [Threat model](docs/threat-model.md)
- [Full v0.1 design](docs/superpowers/specs/2026-08-21-forge-v0-1-design.md)
- [Implementation roadmap](docs/superpowers/plans/2026-08-21-forge-v0-1.md)

## License

Copyright 2026 Clar17y. Licensed under the Apache License, Version 2.0.
