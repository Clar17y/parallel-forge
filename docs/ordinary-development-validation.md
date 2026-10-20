# Ordinary development validation

Issue #130 is based on main `89abee9c76115b317acf1c7818779f15b1b764e8`
(merged PR #127). Validation used Python 3.14.5, Node 24.20.0, PostgreSQL 17,
separate API/worker processes, and the existing controlled Docker runner.

The changed production inputs used by the UI trial have aggregate SHA-256
`ba6106cfd2ae0baf0fa23a7b89bcd05dc2b8a65838a1a5532cd54657c91e577b`.
The loaded runtime inputs are recorded in `source-final-before.json` under
`.llm-output/ui-trial-final/`. Generated development-only `next-env.d.ts`
changes are excluded from that identity and from the candidate.

The final production aggregate is
`87e4fdfd6c35026b6cc422a6bc6cce06afabaf174362076175ec6d4c3ace08dc`,
recorded in `source-final-after.json`. Its only production difference from the
live trial is `domain/run.py`: preference metadata is excluded from lifecycle
snapshot equality. The live trial made no preference edits, so its evidence is
reused for that unchanged path. The concurrent-edit path is proved separately
by the PostgreSQL/controller regression below; it was not repeated with a live
provider or Docker race.

## Focused checks

All pytest commands below used `python -m pytest ... -q` with
`PYTHONPATH=apps/orchestrator/src`. PostgreSQL fixtures created their own
disposable databases. Logs are retained in `.llm-output/backend-finish-checks/`
unless another location is specified. Counts overlap; they are not a total.

| Scope | Result | Retained log |
| --- | --- | --- |
| Ordinary development, run creation, plan gate | 55 passed | `lifecycle-2.txt` |
| Run services, plan approval, ordinary development | 71 passed | `ordinary-final-2.txt` |
| Checkpoint stop/continue and replay, lease renewal, resume replay after preferences | 4 passed | `control-batch-final.txt` |
| Workflow domain, material-question approval refusal, resume controls/boundaries | 21 passed | `resume-final.txt` |
| Preparation, resume controls/boundaries, usage checkpoints, request builder, plan-gate service | 45 passed | `affected-suites.txt` |
| Delegation disabled/requested | 2 passed | `delegation-final.txt` |
| Preference edit during validation; stale preference CAS; version, pause, cancel and acceptance-evidence fences | 6 passed | `preference-identity-final.txt` |
| Epic profile freezing and replay, explicitly asserting unchanged child workflow | 1 passed | `.llm-output/integration-checks/epic-compatibility.txt` |
| Ordinary primary local-candidate guidance, planning and closed-phase tool boundaries | 11 passed | `.llm-output/integration-checks/local-finalization-suite.txt` |
| Web preference editor, creation, plan and status components | 153 passed | `.llm-output/integration-checks/ui-worker-command-evidence.json` |
| Creation lookup failures, retries, explicit override and correction | 30 passed | `.llm-output/integration-checks/new-run-final.txt` |
| Authority/status/context/cockpit copy | 33 passed | `.llm-output/integration-checks/authority-copy-pass.txt` |
| Web preference retry and rejection follow-ups | 47 passed | `.llm-output/integration-checks/ui-followup-pass.txt` |
| Python lint and source typing | Passed; 578 source files typed, final identity delta checked separately | `ruff-final.txt`, `mypy-final.txt`, `preference-identity-ruff-final.txt`, `preference-identity-mypy-final.txt` |
| Web typing/lint, OpenAPI export and generated API agreement | Passed | `.llm-output/integration-checks/` |

Fail-first outputs are retained, including the combined lifecycle failures,
`resume-fail-first.txt`, and the web follow-up failures. The earlier
`unit-api.txt` contains 443 passes and two fixture failures; the repaired
fixtures pass in `ordinary-final-2.txt`. Failed evidence has not been erased.
The final identity regression first produced two expected failures and four
passing control cases in `preference-identity-fail-first.txt`, then all six passed.

## Lifecycle evidence

| Invariant | Concrete coverage |
| --- | --- |
| Ordinary defaults preserve frozen rules | Omitted/null/explicit creation, saved receipt replay, legacy plan gate, frozen profile and epic child tests. |
| Preparation records real owner provenance | Actual preparation handler and same primary; stale evidence checks; internal authority after 55 later events and preference edits. |
| Owner controls retain their causal fences | Pending pause/cancel plus opt-out; paused edits/resume; queued preparation; committed resume replay; checkpoint stop/continue/replay; renewed continuation rejects substituted actor/version/payload. |
| Preference edits preserve accepted lifecycle work | Actual authenticated edit during named-check I/O reaches the local publication gate with persisted controller evidence; stale preference CAS, version, pause/cancel and corrupted acceptance evidence still reject. |
| Missing material input never grants authority | Automatic preparation and human approval refuse unresolved questions; actual owner revision returns to planning without consuming the old gate. |
| Specialists remain optional within owner settings | Primary-only candidate/acceptance/validation; disabled/requested delegation; skip/required review decisions; selected profile remains frozen. |
| Local completion grants no external authority | Failed-check repair requires fresh acceptance before the local publication gate; publication and merge approvals remain separate. |

The combined tests use the actual PostgreSQL services/handlers and controlled
test doubles at provider/runner boundaries. They do not establish live provider
compatibility or external publication. A fresh provider-produced plan after an
owner answers material questions, every epic/control combination, and external
publication/merge remain unproved by these focused checks.

## Real UI trial

Run `779a7b42-d971-439e-8f71-ce6bba10b427` was created through the real new-run
form using the project-default profile and unchanged workflow defaults
(`false / auto / auto`). Its selected primary stayed
`google / antigravity_cli / gemini-3.8-flash-medium`, medium, subscription,
allowance-only, throughout planning, implementation and acceptance.

The primary fixed the missing `+ 32` in `temperature.py` in a Forge-managed
worktree. `test_temperature.py` retained Git blob
`e7104dc673a4771ab9d7f2c824f61ed841dc4a76`; checkout normalized its line endings.
The baseline Docker check failed all three cases. The edited tree passed all
three, and controller validation passed again at local commit
`daf17bc83698e9f349ad48c38cbec618daebd7be`. The worktree was clean, with only
the one-line conversion fix committed.

| Measurement from persisted run creation | Actual result |
| --- | --- |
| First completed Forge tool | 38.33 seconds |
| First useful progress: successful source-file read | 41.49 seconds |
| First source change | 109.59 seconds |
| First passing named check | 118.60 seconds |
| Inspectable tested result in the cockpit | 235.74 seconds |
| Provider attempts | 3, all on the same primary task and route |
| Agent Forge tool calls | 15, including 4 source reads and 2 instruction reads |
| Controller named-check runs | 1, in addition to the primary's before/after checks |
| Child tasks / specialist handoffs | 0 / 0 |
| Required UI operation after entering the task | Create run once |
| Post-creation operator actions / human plan approvals | 0 / 0 |
| Publication approvals / merge approvals / pull requests | 0 / 0 / 0 |

The cockpit showed **Local result ready**, the complete diff, the failed baseline,
the passing edited-tree receipt and the passing committed-candidate validation.
The durable state is `AWAITING_PR_APPROVAL`, with publication still an explicit
owner action. Internal preparation is a worker event bound to the real owner who
created the run; it is not a human plan-approval event. The retained acceptance
and validation manifests agree on candidate, policy and tree identities.

Exact receipts, route records, event history, assertions and screenshots are
retained under `.llm-output/ui-trial-final/`, including
`verified-live-result.json`, `local-result-diff.jpg` and
`local-result-checks.jpg`.

Earlier retained trials include Muse protocol failures, Codex protocol failures,
a fixture missing its `.worktrees/` ignore rule, and a Gemini continuation denied
by its headless client after producing a tested local commit. Their failed
receipts and cancelled runs remain available locally. A subsequent run accepted
and validated an uncommitted fix, exposing a finalization gap: the existing Git
finalizer requires a clean committed candidate. Prompt v20 now tells the ordinary
primary to commit changed owned files locally before closing the candidate, then
take a fresh snapshot; planning and closed-phase tool boundaries are unchanged.
The failing regression and 11 passing affected tests are retained. Provider-adapter changes
and the roadmap's separate Forge-owned publication proof are outside #130.

| Retained earlier run | Attempts / agent tool calls | Outcome |
| --- | --- | --- |
| `0125cbfc` (Muse) | 4 / 18 | Settled protocol failures before writable preparation. |
| `cc7fd441` (Codex) | 4 / 1 | Installed client status/event protocol mismatch. |
| `de9a4b41` (Gemini) | 1 / 4 | Fixture ignore rule prevented worktree creation. |
| `33c6ebfe` (Gemini) | 3 / 19 | Tested local commit retained; headless client denied the acceptance continuation. |
| `8769d14a` (Gemini) | 3 / 13 | Checks passed; uncommitted candidate exposed the finalization gap repaired by prompt v20. |

These runs were cancelled through Forge's UI and retained; none published a PR
or merged anything. The final successful run has no recovery intervention.

## Review and simplification

One integrated independent review covered reuse, quality, efficiency and the
coupled lifecycle boundaries. Its six substantive findings were repaired as one
batch, followed by primary inspection of the repair delta and affected tests.
The simplification pass reused typed workflow values, durable provenance and
continuation helpers, existing delivery comparison, and generated request types.
It adds no second orchestration engine. Jev preparation was partial; it is
advisory and does not establish passing tests or approval to publish. The final
saved diff and its Jev binding are retained in `.llm-output/candidate-final.diff`
and `.llm-output/jev-review-final.json`.

No push, PR creation, hosted CI dispatch, publication or merge is authorized by
this local evidence.
