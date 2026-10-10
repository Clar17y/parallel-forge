# Task Usage Policy Migration: Advisory Monitoring and Optional Hard Caps

Parent: [#94](https://github.com/Clar17y/parallel-forge/issues/94) (Work package U11, GitHub issue #105).

This document audits existing mandatory per-task token and context allowances across Parallel Forge, provides target policy guidance that replaces guesswork with advisory observed averages, and defines the migration path toward optional explicit hard caps.

> [!IMPORTANT]
> **Status: Planned contract and documentation guidance.**
> Runtime monitoring, optional-cap creation, and usage checkpoint pause/resume are planned runtime capabilities delivered in subsequent work packages: backend schema and templates in [#100](https://github.com/Clar17y/parallel-forge/issues/100) (U6), UI editors and forms in [#103](https://github.com/Clar17y/parallel-forge/issues/103) (U9), and integrated runtime verification in [#106](https://github.com/Clar17y/parallel-forge/issues/106) (U12). Ordinary agent policy still inserts finite numeric defaults; subscription profiles already allow optional role caps. Current runtime enforces configured limits. This guide establishes migration expectations without claiming runtime monitoring is already active.

---

## 1. Audit and Inventory of Mandatory Allowance References

`AgentModelPolicy` and ordinary agent requests currently insert finite input and output token defaults. Subscription profiles already allow optional role caps through `TokenBudgetDefaults`. This audit inventories key locations where finite defaults or numeric assumptions remain; it is not an exhaustive list of all usage controls.

### 1.1 Backend Source and API Schema References (Routed to Issue #100 / U6)

The following backend definitions enforce mandatory numeric token limits and must be updated in #100 to make token caps optional:

1. **`apps/orchestrator/src/forge/domain/policy.py`**
   - Lines 124–125 (`AgentModelPolicy`):
     - `max_input_tokens: int = Field(default=100_000, ge=1)`
     - `max_output_tokens: int = Field(default=16_000, ge=1)`
     - *Defect*: Both fields have finite numeric defaults (`100_000` and `16_000`) and enforce `ge=1`. Omitting these fields during policy authoring is currently accepted by Pydantic but silently inserts finite numbers; supplying `None` is rejected because the type is `int` (not `int | None`). Thus, omission cannot currently express an uncapped policy, and finite caps are always imposed.
2. **`apps/orchestrator/src/forge/agents/adk_runtime.py`**
   - Lines 408–409 (`AdkAgentRequest`): dataclass fields typed as `max_input_tokens: int` and `max_output_tokens: int` without defaults.
   - Lines 439–440: `__post_init__` runs `_strict_nonnegative_int32(self.max_input_tokens)` and `_strict_nonnegative_int32(self.max_output_tokens)`.
   - Lines 613–614 (`_over_budget`): halts execution when `usage.input_tokens > request.max_input_tokens or usage.output_tokens > request.max_output_tokens`.
   - *Defect*: The ADK runtime enforces required non-negative 32-bit integer limits and halts if usage exceeds either value.
3. **`apps/orchestrator/src/forge/application/services/planning.py`**
   - Line 466:
     - `token_budget=request.budget.max_input_tokens + request.budget.max_output_tokens`
     - *Defect*: Arithmetic assumes non-null integers and errors if token caps are omitted (`None`).
4. **`apps/orchestrator/src/forge/application/services/plan_evidence.py`**
   - Lines 180–181 & 293–294:
     - `token_budget=policy.planner_model.max_input_tokens + policy.planner_model.max_output_tokens`
     - *Defect*: Rigid addition of input and output token fields assumes mandatory integer values.
5. **`apps/orchestrator/src/forge/application/services/approved_plan.py`**
   - Lines 182–183:
     - `token_budget=policy.planner_model.max_input_tokens + policy.planner_model.max_output_tokens`
     - *Defect*: Requires both token fields to be non-null when building plan budget records.
6. **`apps/orchestrator/src/forge/agents/fake_gateway.py`**
   - Lines 396–397:
     - `step.input_tokens + step.cached_input_tokens > budget.max_input_tokens or step.output_tokens > budget.max_output_tokens`
     - *Defect*: Test fake gateway assumes numeric limits are present on all agent budgets, and additionally illustrates the legacy defect of summing `cached_input_tokens` into `input_tokens`.

*Scope note*: This inventory captures key identified definitions and illustrative defects routed to #100; it is not an exhaustive list of every internal consumer.

### 1.2 Frontend UI and Editor References (Routed to Issue #103 / U9)

The web dashboard and configuration forms currently require operators to manage positive numbers for input and output token limits:

1. **`apps/web/src/components/projects/model-policy.tsx`**
   - Line 22: `defaultModel` defines fallback values `max_input_tokens: 100000, max_output_tokens: 16000`.
   - Lines 150–169: Binds `value={value.max_input_tokens ?? defaultModel.max_input_tokens!}` and `value={value.max_output_tokens ?? defaultModel.max_output_tokens!}` to `TokenBudgetSlider` components with `min={1}`, binding numeric defaults rather than permitting empty/optional values.
2. **`apps/web/src/components/projects/policy-summary.tsx`**
   - Lines 240–241: Table column headers display `['max_input_tokens', 'Input token limit']` and `['max_output_tokens', 'Output token limit']` as standard role settings.
3. **`apps/web/src/app/agents/page.tsx`**
   - Line 25: Renders `<dt>Input / output token limits</dt><dd>{model.max_input_tokens} / {model.max_output_tokens}</dd>`.
4. **`apps/web/src/components/runs/new-run-form.tsx`**
   - Lines 309, 330: Displays role token limits formatted as mandatory numeric values.
5. **`apps/web/src/lib/api/schema.d.ts`**
   - Lines 1669–1676: Generated TypeScript interface types `max_input_tokens: number;` and `max_output_tokens: number;` with `@default 100000` and `@default 16000`.

*Scope note*: This inventory identifies key UI bindings routed to #103; it is not an exhaustive list of all frontend elements.

### 1.3 Conceptual Examples vs. Measurements

Documentation and test files contain example figures such as `200k`, `400k`, `262,144`, or `65,536` tokens (e.g., in probe test logs in [Client capabilities](v0.2-client-capabilities.md)).
- **These numbers are illustrative examples or historical test-fixture parameters, NOT empirical baselines or recommended defaults.**
- Operators and authors must not treat example numbers as measurements of real-world task requirements.

---

## 2. Planned Target Policy Guidance: Advisory Monitoring and Optional Hard Caps

Under the target usage monitoring contract, per-task token limits are no longer mandatory. The default authoring workflow omits token allowances entirely.

### 2.1 Omitted Allowance Fields by Default

New project policies and task creation requests should omit per-task token allowance fields (`max_input_tokens`, `max_output_tokens`).

#### Conceptual Target Policy Configuration (Supported Current Wire Fields)

Today, this example still receives the existing finite defaults. Omission will mean no explicit token cap only after the backend and editor migration is delivered.

```json
{
  "planner_model": {
    "provider": "google",
    "model": "gemini-3.5-flash",
    "reasoning_effort": "medium",
    "max_tool_calls": 100,
    "max_duration_seconds": 1800,
    "max_cost_minor": 1000
  },
  "developer_model": {
    "provider": "google",
    "model": "gemini-3.5-flash",
    "reasoning_effort": "medium",
    "max_tool_calls": 120,
    "max_duration_seconds": 3600,
    "max_cost_minor": 2000
  },
  "reviewer_model": {
    "provider": "google",
    "model": "gemini-3.5-flash",
    "reasoning_effort": "low",
    "max_tool_calls": 50,
    "max_duration_seconds": 1800,
    "max_cost_minor": 1000
  }
}
```

No numeric token allowance is required. Workflow execution is governed by:
- Duration bounds (`max_duration_seconds`);
- Tool call limits (`max_tool_calls`);
- Cost limits (`max_cost_minor`);
- Named check and remediation limits (`local_remediation_limit`, `remote_remediation_limit`);
- Active telemetry monitoring and usage checkpointing.

Optional explicit caps use planned conceptual fields (`TaskAllowanceInput` / `MonitoringPolicy` in #95):

```json
{
  "hard_token_cap": 500000,
  "hard_context_cap": 128000
}
```

### 2.2 Semantics of Optional Hard Caps

Hard token caps are deliberate, advanced settings:

- **Positive Integers Only**: Any explicit hard cap (`hard_token_cap`, `hard_context_cap`) must be an integer from `1` through `9223372036854775807`, matching PostgreSQL `BIGINT` storage.
- **Absent / `null`**: No explicit hard cap for that dimension. Usage is observed and compared against advisory baselines; checkpointing depends on the selected monitoring mode.
- **Strict Rejection**: Values of `0`, negative integers, booleans, and integers above the storage range are rejected by domain and schema validation (`TaskAllowanceInput`, `MonitoringPolicy`).
- **Zero Semantics**: Measured usage may be zero. New explicit hard caps reject zero; existing subscription records that legally contain a zero input or output ceiling retain that value during legacy decoding.
- **Soft Defaults and Owner Edits**: An optional hard cap is an owner-configured setting, not an immovable barrier immune to owner edits. An authenticated owner can adjust, raise, or remove the cap on the policy or run at any time.

### 2.3 Legacy Finite Caps

Existing project policies and immutable profile records containing finite token caps (such as `100_000` / `16_000`) will continue to be respected as explicit hard caps until an authenticated owner directly updates the policy. Migration will not silently erase or modify existing caps.

Foundation migration `0038` can roll back to `0037` while its monitoring tables are empty. Once a work unit, policy revision or other monitoring evidence has been recorded, downgrade refuses to discard it and leaves the schema revision and records intact. Stop writers and preserve measured usage and owner-action history when planning recovery.

---

## 3. Observed Cross-Project Averages vs. Guesstimates

Instead of requiring operators to guess token consumption before a task runs, Forge uses observed historical averages across accessible projects to provide realistic expectations.

### 3.1 Accessible Projects and Breakdown

Aggregations span only ACCESSIBLE projects within authorized tenancy boundaries. Telemetry is segmented by:
1. **Admitted Task Category and Phase**: Derived from evidence-based execution metadata (e.g., admitted `phase_id` such as `plan`, `implement`, `review`). If a category cannot be determined from evidence, it remains honestly labeled `unknown` (never guessed or defaulted).
2. **Multi-Agent Route Composition**: Segmented by configured and effective `RouteCohort` members (`role`, `provider`, `model`, `ordinal`, `reasoning_effort`) and their SHA-256 fingerprint.
3. **Model Identities**: Based on the providers and models configured for the admitted work.

### 3.2 Projected Metrics

Projections report:
- `mean`, `median`, `p90`, `sample_count`, `coverage`, and `freshness`.
- No empirical 45,000-token defaults or arbitrary baselines are assumed.

### 3.3 Planned Monitoring Policy and Baseline Evaluation

- **Rolling History Limit**: Proposed limit of 50 recent completions (`history_limit = 50`).
- **Minimum Comparables**: Proposed minimum sample size of 20 comparable completions (`minimum_comparables = 20`).
- **Editable Modes**: `report_only`, `warn`, and `checkpoint` (editable soft defaults).
- **Activation**: The shared foundation's policy constructor defaults to `report_only` and does not activate monitoring. The parent plan proposes `checkpoint` for new project policies, with reporting while comparable history is insufficient. Existing projects change only when the owner selects the new policy; owners can choose any supported mode.
- **Threshold Multipliers**: Warning multiplier default `1.5x`; checkpoint multiplier default `2.0x`. Evaluator baseline evaluation proposes `max(2 * median, p90)` when comparable history exists.
- **Frozen Reference at Admission**: A `BaselineReference` is frozen at admission time; subsequent runs or policy edits do not rewrite it for an in-flight work unit.
- **Sparse History Fallback**: When fewer than 20 comparable completions exist (`status = "insufficient_history"`), `reference_tokens` remains `None`, and monitoring automatically falls back to `report_only`.

### 3.4 Averages vs. Subscription Quota

An observed cross-project task average is an empirical consumption metric (how many tokens tasks of this type typically take). It **must never be confused with remaining subscription allowance**:
- Subscription quotas represent external billing window limits on personal or team accounts (e.g., Anthropic weekly allowance or ChatGPT rate tiers).
- An average does not indicate remaining provider subscription quota.

---

## 4. Cumulative Task Tokens vs. Model Context Occupancy

It is vital to distinguish between total tokens consumed over time and immediate context window occupancy:

| Dimension | Scope | Definition | Controls & Protections |
| :--- | :--- | :--- | :--- |
| **Cumulative Task Tokens** | Entire task lifecycle | Total count of `input_tokens + output_tokens` consumed across all turns, tool interactions, retries, and child delegations. `cached_input_tokens` is a subset of input; `reasoning_output_tokens` is a subset of output. Total is NEVER `input + output + cached + reasoning`. | Monitored advisory averages; optional explicit hard caps; usage checkpoints. |
| **Model Context Occupancy** | Single invocation turn | Current occupancy within the model's physical window (`occupied_tokens`, `capacity_tokens`, `compaction_count`). | Physical model limit; provider compaction and headroom strategies. |

Key invariants preserved:
- **Cumulative Definition**: Total cumulative tokens is strictly `input_tokens + output_tokens`. Cached input is a subset of input; reasoning output is a subset of output. Never calculate total tokens as `input + output + cached + reasoning`.
- **Aggregation Integrity**: Multi-source aggregation spans reconciled sources, retries, and child delegations across the task lifecycle without double-counting provisional live events and final reconciled receipts (`reconciles_event_id`).
- **Unknown Telemetry**: Missing or unmeasured dimensions are explicitly recorded as `unknown` (e.g., via `unknown_dimensions`) or treated as lower bounds, never assumed to be zero.
- **Context Constraints**: Model physical context limits remain hard architectural boundaries on single-invocation prompts.
- **Deferred Automatic Context Checkpoints**: Automatic context-based checkpointing is deferred; runtime checkpointing focuses on task-level cumulative tokens.
- **Compaction and Headroom**: While providers or external adapters may utilize prompt compaction or headroom reservations, Forge does not currently implement automatic compaction or headroom enforcement; documentation must not imply Forge implements these mechanisms.

---

## 5. Usage Checkpoints and Direct Authenticated Continuation

When monitoring detects unexpected usage or approaches a configured checkpoint threshold:

1. **State Preservation**: The worker captures an immutable checkpoint receipt in PostgreSQL, recording cumulative tokens, tool calls, elapsed time, and the latest evidence digest across checkpoint states (`checkpoint_requested`, `pausing`, `paused`, `resumed`, `resolved`).
2. **Direct Authenticated Continuation**: An authenticated owner can authorize continuation directly with a single action:
   - No second permission request or separate proof workflow is required.
   - Explanatory notes are optional, not compulsory.
   - Continuation does not reset historical usage or alter durable task identities.
   - Continuation does NOT consume local or remote remediation allowances (`local_remediation_limit`, `remote_remediation_limit` govern check failures and code repair iterations, not monitoring checkpoints).
   - Safe `paused` and `resumed` states require confirmed process settlement before resumption.
3. **Truthful Stopping Constraints**: Independent hard limits remain authoritative and will pause or halt execution regardless of monitoring policy:
   - Explicit operator cancellation or stop commands;
   - Confirmed provider quota exhaustion or billing window blocks;
   - Worker lease timeouts and unsettled process trees;
   - Immutable security and tool boundary violations.

---

## 6. Static Role Instructions Audit

The static role instructions bundled in Forge were audited for mandatory token or context allowance wording:

- **`agents/planner/instructions.md`** (Version 2): No token allowance wording. Focuses on read tools, structured `PlanOutput`, and dependency change specifications.
- **`agents/developer/instructions.md`** (Version 5): No token allowance wording. Focuses on managed worktree operations, plan execution, and `DeveloperOutput`.
- **`agents/reviewer/instructions.md`** (Version 1): No token allowance wording. Focuses on independent verification, findings classification, and `ReviewOutput`.

**Conclusion**: Bundled role instructions contain no mandatory numeric allowance wording and require no modification. Preserving these instructions intact ensures active instruction identities remain frozen and unperturbed across running tasks.

---

## 7. Migration Checklist for Related Features

| Issue | Work Package | Component | Planned Action |
| :--- | :--- | :--- | :--- |
| **#95** | U1 | Storage & Contracts | Additive task usage observation schema, baseline snapshots, checkpoint records. |
| **#100** | U6 | Backend & API | Make `max_input_tokens` and `max_output_tokens` optional (`int | None = None`) in `AgentModelPolicy`, `AdkAgentRequest`, and planning services. |
| **#103** | U9 | Web Dashboard | Update `ModelPolicy`, `PolicySummary`, and run forms to make token limits optional fields; display advisory averages. |
| **#105** | U11 | Documentation | Audit existing references, publish migration guide, update architectural docs, annotate v0.2 history. *(This document)* |
| **#106** | U12 | Integration | Validate end-to-end task execution without mandatory token caps; verify schema fixtures and live projections. |

---

## 8. Related Documentation

- [Architecture Overview](architecture.md)
- [Threat Model](threat-model.md)
- [Per-Run Profile Overrides](run-profile-overrides.md)
- [Subscription Runtime Specification](v0.2-subscription-runtime.md)
- [Local Profile CLI Guide](v0.2-profile-cli.md)
- [Subscription Client Capabilities](v0.2-client-capabilities.md)
