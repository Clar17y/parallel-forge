# Forge repository guidance

## Development agent framework

Use the installed personal multi-provider framework for repository coding work.
The routing policy is the managed framework block in
`C:/Users/sdyer/.codex/AGENTS.md`; invocation and recovery instructions are in
`C:/Users/sdyer/.codex/agent-framework/README.md`, with provider pins in
`C:/Users/sdyer/.codex/agent-framework/routing.json` and native role definitions
in `C:/Users/sdyer/.codex/agents/`. Follow those installed sources when routing
changes; the following summarizes the current version 4 policy.

- The primary agent retains the user's selected model and owns architecture,
  decomposition, integration, and final synthesis.
- Route routine implementation through `ask-gemini` first: `agy` with
  `gemini-3.8-flash-medium`. Only confirmed quota exhaustion reported as
  `fallback_required` (exit 20) permits automatic handoff to a fresh native
  `implementer` on `gpt-5.6-luna`, medium. Preserve the contract, partial edits,
  prior output, and unfinished checks; ensure the previous writer has stopped.
  Authentication, permission, timeout, model, and transient network failures
  are not quota exhaustion. Follow the framework's pending-session recovery
  rules before retrying uncertain termination.
- Use `complex_implementer` (Terra low) for complex bounded implementation;
  `planner` and `test_engineer` (Sol low) for planning and adversarial tests;
  and Luna medium for exploration, documentation, and refactor audits.
- Classify risk once with a reason: low documentation/formatting/behavior-preserving cleanup uses direct checks; ordinary behavior changes use relevant tests and one integrated batch review; high-risk work uses that batch review plus the scoped Sol-high correctness gate after repairs. Track findings by stable ID, and after two unsuccessful repair rounds record the primary reassessment and next action.
- The primary runs short checks. Use a verifier only for substantial independent verification or an explicit repository requirement, recording candidate HEAD and complete uncommitted identity before and after. Workers report completion, blockers, interface decisions, owned paths, and checks without routine status chatter, polling, or automatic extra agents. Records may be swept by scratch cleanup and must be reconstructible and credential-free.
- Independent review uses `ask-claude` in fresh context with exactly
  `claude-opus-5`. If unavailable, use a fresh `reviewer` (Astra low) and
  disclose the fallback. Never route to Sonnet or Fable. Claude receives only
  read/search tools, with no MCP, shell, edit, or subagent tools.
- High-risk concurrency, cancellation, timeouts, security, migrations, data
  integrity, and release work require a separate `correctness_gate` (Sol high)
  after repairs. Explicit security analysis uses `security_reviewer` (Sol high).
  Luna max is reserved for exceptional, explicitly requested deep review.
- Use a verifier only for substantial independent verification or explicit repository requirements. Record
  candidate HEAD and the complete uncommitted identity. The integrated batch
  review must be independent of its writers; changed code requires current
  evidence. Provide Claude the base revision and a saved integrated diff.
- Delegate bounded contracts with acceptance criteria, non-overlapping owned
  paths, and validation commands. Subagents may not delegate again without
  parent authorization. Use installed native roles where supported; otherwise
  pass their exact model, effort, and instructions. Respect host concurrency
  limits.
- Keep task contracts and provider logs in the active managed worktree's
  `.llm-output/`. Preserve partial edits and logs. User-authorized provider CLI
  bypass flags do not bypass Codex sandbox or automatic approval review.

This framework routes the development assistants working on this repository.
It does not grant capabilities to the Forge application's runtime agents.
Repository worktree rules, TDD, CI, controlled runtime tools, and human release
gates remain binding.

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
