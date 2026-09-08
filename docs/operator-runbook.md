# Forge operator runbook

This describes the implemented v0.1 controls. End-to-end acceptance remains in
progress; consult [the evidence ledger](v0.1-progress.md) for current checkpoints
and open gates.

## Instance and startup

Use Python 3.14, Node.js 24, PostgreSQL 17 and Docker. Install locked dependencies
with `uv sync --frozen --extra dev` and `npm ci`. For local development,
`docker compose up -d postgres` provides PostgreSQL on loopback port 5435.

The API, worker and CLI must share `FORGE_DATABASE_URL` and `FORGE_DATA_ROOT`.
This is the control database; a managed worktree's isolated application database
is a different resource. Preserve the control database, protected secrets,
manifests and artifacts together. Retain the existing development runtime when
resuming this repository.

Run `uv run --frozen alembic upgrade head` against the intended control database.
Start each process in a separate terminal from the configured checkout:

```text
uv run --frozen forge-api
uv run --frozen forge-worker
npm run dev:web
```

The worker requires a provider secret reference, pricing catalog and versioned
prompts. Wait for its recovered-and-polling message; a working API alone does not
prove commands are being executed. A one-command development supervisor remains
an acceptance gap.

The default browser origin is `http://127.0.0.1:3000`; the server-side web proxy
uses `http://127.0.0.1:8000` internally. When changing ports, configure
`FORGE_WEB_ORIGIN` consistently and `FORGE_API_INTERNAL_ORIGIN` on the web server.
Keep the instance on loopback and credentials out of browser configuration.

Run `uv run --frozen forge operator rotate` to revoke existing local sessions and
obtain a fresh bootstrap URL. Open that private URL once. Its fragment contains
the short-lived token; do not share it. The dashboard exchanges it for an HttpOnly
session and removes the fragment. A stale link requires rotation.

## Credentials and project policy

Provider credentials use `secret://forge/<secret-id>` references in
`LocalSecretStore`. `FORGE_PROVIDER_SECRET_REFERENCE` selects one;
`FORGE_GOOGLE_API_KEY_REFERENCE` must not conflict with it.
`FORGE_PRICING_CATALOG_PATH` selects the versioned pricing catalog. Raw keys must
not appear in task text, policy, artifacts or agent requests. The current store
exposes an operator `create` adapter; a general secret-management CLI is not
implemented. Obtain values interactively and configure only their references.

`FORGE_GITHUB_TOKEN_REFERENCE` accepts a protected local reference or an explicit
`env://VARIABLE_NAME` server-side reference. Limit its repository access. PR writes
need Pull requests write, while merging requires Contents write. Classic branch
protection inspection requires Administration read. Do not grant bypass or
Administration write to make a rejected preflight pass. Consult GitHub's
[PR permissions](https://docs.github.com/en/rest/pulls/pulls) and
[protected-branch permissions](https://docs.github.com/en/rest/branches/branch-protection).
Forge may establish protection through verified ruleset/installation observations;
an unverified result does not authorize merging.

Register the canonical Git repository in Projects and save a versioned policy.
Review repository identity, default branch, allowed paths, named commands,
required checks, budgets, runner mode and environment allowlist. Policy updates
create a new version rather than rewriting approved evidence.

Docker is the default. Build `Dockerfile.runner` and set `FORGE_RUNNER_IMAGE` to
its immutable `sha256:` image ID or repository digest; mutable tags are refused.
The runner mounts only the canonical managed worktree, runs as UID/GID 10001,
receives allowlisted values and defaults to no network. Trusted-host mode is
explicitly unsandboxed and only for an operator-designated trusted project.

## Workflow and approvals

Create a task and inspect its run cockpit. The API records commands; the separate
worker performs them. Resumable SSE reports activity and reconnecting clients
reload persisted state. Check command outcome and run version before retrying a
request; a button click does not prove a remote effect succeeded.

1. **Plan approval:** inspect frozen scope, checks, risks and policy. Approve the
   displayed plan or request revision. Approval allows preparation and scoped work.
2. **PR publication approval:** inspect the exact local commit, diff, validation
   and independent review. The Release Controller may then push and create or
   reconcile the managed PR.
3. **Merge approval:** inspect the exact remote head, observed base, checks,
   reviews and protection. Changed evidence requires fresh approval. Agents cannot
   approve or merge their own work.

Remediation is bounded by policy. Queue admission is not completion: completion
requires a canonical merged PR receipt and merge SHA. See
[queue behavior and recovery](merge-queue-implementation.md). An uncertain enqueue
or merge response is not proof that nothing happened remotely.

## Pause, cancellation and restart

Use state-aware cockpit controls. Pause may wait for an admitted operation to
settle; it does not erase receipts. Cancellation is refused during merging,
including a paused or intervened merging phase. Do not change database state to
bypass that boundary.

Resume through the supported control after inspecting evidence. Recovery retains
original command and receipt identities and must not repeat uncertain mutations.
Inspect intervention reasons before authorizing continuation. Resume is not a
fresh merge approval.

The worker recovers durable state before admitting normal work. Recovery failure
exits nonzero and keeps admission closed. Preserve evidence, resolve the reported
dependency or integrity failure, then restart. Do not manually clear leases,
intents, quarantines or recovery barriers merely to resume polling. See
[recovery procedures](recovery-environment.md) for manifest failures and historical
environment details.

## Resource retention and teardown

Completion and cancellation retain worktrees, databases and evidence. Teardown
requires an explicit operator request and current resource identity. The current
run teardown flow is terminal-state gated; intervention teardown acceptance is
still open. Partial teardown records which resources remain.

Standalone development worktrees use the setup/teardown wrappers in `scripts/`.
Use their `--help` for the current CLI contract and point lifecycle operations at
the control database containing the project registration. Use the managed
lifecycle rather than manually removing computed paths or databases.

## Rotation and incidents

Use `forge operator rotate` to revoke local sessions. For a compromised provider
or GitHub token, revoke it at its issuer, create a replacement under a fresh
protected reference, update configuration and restart affected processes. Retain
historical evidence and avoid overwriting an immutable secret ID.

Before repair, preserve the relevant control-database backup and protected copies
of manifests, artifacts and bounded logs. Record commit, run, command and operation
IDs, versions and the last successful step. Keep credentials and bootstrap URLs
out of shared reports. Inspect GitHub read-only when a remote receipt is uncertain.
Development PRs still require fresh, immediate human authorization before merging.

## Verification limits

Default tests and CI exclude live model and GitHub writes. Evaluation fixtures and
metrics exist; service/CLI integration is being completed. Consult the ledger
before relying on an evaluation command. Hosted Windows/Linux checks, complete
process acceptance, Playwright accessibility coverage and final acceptance mapping
remain required before v0.1 can be declared complete.
