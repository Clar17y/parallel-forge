# Jev advisory search and review

Jev is an optional advisory service for semantic search and review focus. A subscription profile can supply a reusable default for all projects using it. Explicit project Jev settings, including `off`, override the profile default. Each run uses its immutable project policy and bound profile version; changing a profile or project selection does not change an existing run. When neither supplies a Jev setting, legacy global search-ranking behavior remains unchanged.

## Enable once in a subscription profile

1. Supply `TYPESAFE_API_KEY` to the Forge worker process and restart that worker. The API process only reads the persisted report.
2. Open **Subscription profiles**, create or edit a profile, and select **Configure Jev default**.
3. Choose **On** (or **Shadow** for measurement), allow remote processing, and save the profile version.
4. Select that profile version for the projects that should share its defaults. Leave **Configure Jev for this project** unset to inherit the profile setting. Start a new run to use the saved configuration.

The profile default includes the Jev model alias and limits. Existing project overrides remain in force, so a project explicitly set to Off stays off. Clear **Configure Jev for this project** and save a new policy version to return to inheritance. Runs without a bound subscription profile do not inherit a project's currently selected profile retroactively.

## Enable for a project

1. Supply `TYPESAFE_API_KEY` to the Forge worker process and restart that worker. The API process only reads the persisted report.
2. In the project's policy settings, configure Jev, choose `shadow` or `on`, and allow remote processing. Keep `jev-latest` or enter another model alias; Forge does not require a version pin.
3. Save the policy and start a new run. Inspect **Usage → Jev advisory usage**, or run `forge jev report RUN_ID --json`.

To disable an existing configuration, save a policy with Jev mode `off`. Existing runs retain their bound policy version. Unconfigured legacy search-ranking activity continues to use the separate legacy ranking telemetry.

Profile and project defaults apply to future runs. The worker needs `TYPESAFE_API_KEY`, and remote processing requires explicit `allow_remote` consent in the effective configuration. Existing run policies and reports remain bound to their saved versions.

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
