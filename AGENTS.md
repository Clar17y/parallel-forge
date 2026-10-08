# Forge repository guidance

## Development agent framework

Use the installed personal multi-provider framework for repository coding work.
The current routing and recovery policy lives in
`C:/Users/sdyer/.codex/AGENTS.md`, with invocation instructions in
`C:/Users/sdyer/.codex/agent-framework/README.md` and provider pins in
`C:/Users/sdyer/.codex/agent-framework/routing.json`. Follow those installed
sources instead of copying a stale routing policy into this repository.

- The primary retains the user's selected model and owns scope, architecture,
  integration and final acceptance. Small understood fixes may be implemented
  directly with focused checks and self-review.
- Delegate substantial bounded outcomes with fresh compact contracts, explicit
  non-overlapping ownership, acceptance criteria and validation commands. Workers
  own investigation, implementation, repairs, checks and self-review. No nested
  delegation unless the primary authorizes it.
- Routine delegated implementation uses `ask-gemini` first. Use the installed
  fallback rules for exhausted, unavailable or blocked routes; preserve partial
  work and confirm the previous writer has stopped. Do not relabel other errors
  as quota exhaustion.
- Choose review and verification depth for actual risk and uncertainty. Prefer
  one independent integrated review when useful; do not commission reviews for
  each small edit. Follow `ask-claude` and the installed exact model pins when
  selecting independent review. Use additional scoped gates when they answer
  distinct unresolved correctness or security questions.
- Reuse matching credible test evidence. Rerun affected checks after relevant
  changes or failures; do not repeat full suites for documentation/checkpoints.
  Full CI dispatches and reruns require explicit authorization for that specific
  run, including runs triggered by opening or updating a PR. Merge authorization
  remains separate and immediate.
- Preserve `.llm-output/` contracts, logs and partial edits locally. One owner
  monitors each delegated process or CI run using the installed backoff policy.
  Never reset or clean another writer's work.
- For substantial stateful changes, record critical invariants, transitions and
  combined boundary cases in the existing task contract before implementation.
  Keep a compact coverage map with planned checks, concrete candidate-specific
  results and explicit unproved cases. The primary checks the evidence before
  accepting the outcome; a populated map does not establish completeness.
- Consolidate related defects into one repair batch for the owning component.
  After a second related defect during implementation, validation or review,
  reassess remaining paths before another patch. For complex repairs, checkpoint
  a stable candidate after focused tests and the selected self-review, then run
  required broad checks on that final candidate and reuse unchanged evidence.

This framework routes the development assistants building Forge. It does not
configure the Forge application's runtime roles or grant them unrestricted
shell, provider credentials, CLI bypass flags or release authority. Subscription
runtime adapters must preserve the runtime boundaries below.

### v0.3 release acceptance

The owner may accept the recorded local acceptance checks and matching existing
hosted evidence in `docs/v0.3-validation.json`, or choose an additional hosted
full CI run. A new full CI run is optional for v0.3 shipping. Record the owner's
choice, the tested source snapshot, actual results and remaining unproved cases
in the release evidence; retain failed receipts and identify unrun checks.

For PR #88, the owner's 2026-10-08 instruction selects the recorded evidence
and declines further CI. Documentation-only updates of that verified source
use `[skip ci]` to avoid starting Actions runs. Codeowner review, resolved review
conversations and immediate explicit human merge authorization still apply.

## Owner overrides in every plan

- Workflow controls are soft defaults. Every planned approval, readiness,
  sequencing, deferral, budget, retry or review restriction must include an
  explicit authenticated-owner override or an owner-editable setting.
- The owner's action authorizes the concrete requested operation. Do not add a
  second permission request, compulsory justification or separate proof workflow
  merely to override a recommendation. Notes are optional.
- Record the owner, action, actual warnings and affected snapshot. Preserve real
  usage and evidence; an override never turns an unrun check into a passing
  check, unknown spending into zero, or an unverified dependency into verified
  integration. Show the override in the operation's normal projection.
- Keep inputs and durable identities accurate. Resolve stale edits or interrupted
  effects with an explicit owner recovery action that preserves existing work.
- Apply this policy to all new plans and to controls changed by the current
  feature. Include default and owner-override behavior in its focused tests.

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
- Local CLI providers are for personal operator use. The default admission
  policy trusts configured clients; capability evidence is optional. Missing
  proof and ordinary client updates must not disable an otherwise usable
  adapter. Preserve honest `operator_trusted` reporting and Antigravity's
  `approved_tools_unproved` warning. The CLI may expose native tools beyond
  Forge's interface; this accepted limitation is not an admission gate.
- An approved run permits its primary to delegate to configured runtime agents
  within the run's roles, ownership and budgets. No separate capability-proof
  workflow or human approval is required for each child. Keep actual callback
  checks, spending settings, quota backoff, cancellation and process settlement.
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

These restrictions govern the Forge application's runtime agents and command
runner. Development assistants use the framework above to implement and verify
the repository; provider routing does not relax the runtime restrictions.

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
