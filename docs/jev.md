# Jev advisory search and review

Jev is an optional, per-project advisory service for semantic search and review focus. Its policy is stored in each immutable project policy version, and each run report uses the policy version bound to that run. A project with no `jev` policy keeps the legacy global search-ranking behavior; editing an unrelated field must not add an explicit Jev policy.

## Enable for a project

1. Supply `TYPESAFE_API_KEY` to the Forge worker process and restart that worker. The API process only reads the persisted report.
2. In the project's policy settings, configure Jev, choose `shadow` or `on`, and allow remote processing. Keep `jev-latest` or enter another model alias; Forge does not require a version pin.
3. Save the policy and start a new run. Inspect **Usage → Jev advisory usage**, or run `forge jev report RUN_ID --json`.

To disable an existing configuration, save a policy with Jev mode `off`. Existing runs retain their bound policy version. Unconfigured legacy search-ranking activity continues to use the separate legacy ranking telemetry.

## Modes and source consent

- `off` disables Jev work.
- `shadow` records advisory results and projected ranking while preserving the baseline results delivered to the agent.
- `on` applies the advisory ranking within the configured result limits.

Remote source processing is separately controlled by `allow_remote`. When enabled, Jev may receive bounded, redacted excerpts and task context for configured semantic search or review focus. Do not enable it unless the project owner accepts that egress. The operator settings contain a free-text model alias (default `jev-latest`); the alias is passed through without a Forge model pin or whitelist.

Each policy version bounds top results, requests, input allowance, candidates, result characters, timeout, and cache lifetime. Input allowance is deliberately conservative: one unit per serialized request byte, increased if the reported input token count is higher, and never refunded after an admitted call. Reported token usage is a separate measure. The default 250,000-unit allowance may permit only a few large requests; increase it explicitly for larger runs. Both limits apply, so a 64-request cap does not promise 64 calls.

Exhausted budgets and local request-limit refusals are recorded without provider charges. Unavailable credentials, timeouts, and provider errors produce visible unavailable or unknown outcomes. The run report separates configuration, observed availability, provider calls, cache hits, reported tokens, reserved allowance, remaining budgets, and diagnostic reasons. It reports no money amount when no price evidence exists. The returned-text limit does not invalidate otherwise valid provider scores; transport and score validation have separate hard bounds.

Cache entries are scoped to their request and model alias. A successful response is reused until its configured TTL expires; set `cache_ttl_seconds` to zero to disable reuse. Changing the model alias or request contents naturally produces a different cache key. Shadow mode can have provider cost even though its ranking is not applied.

The advisory review focus can direct attention, but it never proves a defect and never bypasses the normal review, validation, approval, or merge gates. Treat partial coverage and unknown outcomes as incomplete evidence.

The transport follows the [TypeSafe HTTP API contract](https://docs.typesafe.ai/api), including Score answers and reported token usage. Contract tests use a simulated transport; no live provider evaluation is required for local tests.

## Evaluation

Compare `off`, `shadow`, and `on` using the same task fixtures and equal task-success criteria. Include failures, unknown outcomes, latency, cache behavior, and actual usage in the comparison. Do not compare modes only by calls avoided, and do not claim savings without recorded price evidence. The operator command `forge jev report RUN_ID` prints a run's safe persisted projection; `--json` is suitable for evaluation tooling.
