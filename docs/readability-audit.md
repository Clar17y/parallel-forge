# Readability audit

The original browser walkthrough covered all 19 page types, every epic view and all nine run sections. It captured 82 screenshots. Additional local test data covers populated plan, checks, review and approval evidence, a long accepted requirements brief and graph, and evaluation results.

The main issue is information hierarchy: repeated identifiers, historical records and diagnostic explanations compete with the current status and next action. Expandable sections help when their visible summary is useful on its own. Comparison data still belongs in tables, and long editable text needs wrapping fields.

The implementation and independent code review are complete, including repairs for the review findings. The resolutions below describe the implemented changes. The local app is running; final browser verification of the optimized build remains pending restoration of browser control.

| Page | Problem observed | Resolution |
| --- | --- | --- |
| Home | Separate full panels repeat empty queue messages. | Compact inbox with useful counts, pending work and next steps. |
| Agents & models | Every role exposes limits, prompt identities and provenance alongside the selected model. | Role summaries show model, provider and reasoning; limits and provenance expand independently. |
| Subscription profiles | Profiles, client health, immutable versions and historical worker instances form one long page. | Separate those tasks, summarize role routes and keep historical details secondary. Preserve current health, stale data and client limitations. |
| Settings | Navigation cards contain more explanation than users need to choose a task. | Short task titles and one useful sentence per destination. |
| Policies | Existing disclosures work well, but selectors and version metadata remain verbose. | Reuse the disclosure pattern with compact selectors and structured details. |
| Tool permissions | Raw capability identifiers repeat under each role. | A capability comparison with exact technical identifiers available in details; keep the native CLI limitation visible. |
| Evaluations | Empty pagination provides no next step. Populated cases repeat suite IDs and version paragraphs. | Actionable empty state and compact case/result/role summaries, with expandable metrics and provenance. |
| Audit | UUID filters and repeated event context dominate the timeline; expanded payloads can widen the page. | Compact events and filters, secondary identifiers and bounded evidence. |
| Usage | Many unknown fields, compound baseline identifiers and route cards obscure measurement coverage. | Clear sections and comparison tables; concise coverage, with provenance and baseline details available separately. Unknown remains distinct from zero. |
| Projects | The table is already compact, but policy version labels lack context. | Preserve the table and clarify version labels. |
| Register project | Introductory prose and serial full-width fields make a basic task look large. | Group basic fields, shorten help and disclose optional configuration. |
| Project detail | Policy, subscription profile and monitoring editors all open by default. | Start with the saved configuration summary and explicit edit controls that preserve drafts and request identity. |
| Epics | Long titles squeeze state and actions in the list. | Stable columns and wrapping titles with clear status and actions. |
| New epic | The basic flow is already simple. | Keep the short idea-first form and concise guidance. |
| Epic workspace | Mature epics retain introductory prompts; long requirements and criteria are clipped; history and diagnostics repeat. | Stage-aware defaults, readable accepted content, wrapping editors, independently expandable items and compact delivery evidence. See the view coverage below. |
| Runs | Nine columns repeat absent values, zeros and unwieldy durations. | Useful primary columns, human duration units and secondary details. Distinguish completed and cancelled work. |
| New run | Full role strings, profile UUIDs and API model policy explanations compete with the selected execution mode. | Summarize the actual requested profile and distinguish CLI configuration from API policy. Make limits and checks secondary. |
| Run workspace | The shared header dominates every section. Tasks sit below usage, and attempt diagnostics repeat. An ended execution can appear active. | Compact shared context, truthful status, task-first presentation and expandable technical evidence across all nine sections. |
| Approvals | An empty list leaves redundant pagination and little guidance. | Clear empty-state navigation; populated approvals retain their stage, evidence and review action. |
| Event evidence destination | The audit link opens a raw JSON response without surrounding context. | An authenticated in-app evidence reader with formatted, bounded JSON and access to the original response. |

## Epic view coverage

- **Brainstorm:** distinguish a current proposal from an adopted one; make old conversations and job telemetry secondary.
- **Brief:** lead with the accepted brief when available; provide readable requirements and criteria and preserve edited drafts.
- **Graph:** show title and deliverable before criteria, expose requirement text alongside stable bindings, and separate accepted content from authoring history.
- **Delivery:** summarize repeated blockers with affected items, compact attempts and budgets, and retain current warnings, owner actions and unsettled effects.

## Run section coverage

Overview, Plan, Changes, Checks, Review, Tasks, Activity, Usage and Security are separate inspection targets. Empty sections are checked alongside populated evidence. Changing the visual hierarchy must preserve exact approval bindings, current candidate/check identity, pending operation recovery and the difference between measured, unknown and stale data.

## Verification record

After integrating current main and the further simplify repair, the local web suite passed all 1,125 tests across 108 files. The optimized production build, TypeScript, lint, frozen API schema check and all 29 presentation contract checks also passed on that combined source. Current main's shared task controller, refresh wiring and visible queue diagnostics are preserved alongside the compact task layout.

The earlier independent review covered the complete integrated readability diff, including reuse, quality, efficiency, React accessibility and state preservation. Its two confirmed findings were repaired: activity filters now use recorded outcomes for failed checks, timeouts and denied calls, and policy token limits retain exact signed-64 values. All 52 affected tests passed after those repairs, including four new regression cases; those cases are also part of the final complete web suite.

The first hosted Codex review found that a short accepted requirement could still be clipped in its one-line summary without appearing in the expanded body. Every requirement card now includes its complete text when expanded. The updated regression first failed on the missing short-text body, then all 35 affected epic tests passed, covering short, exactly 80-character, multiline and long requirements. TypeScript, scoped lint and the optimized production build also passed after this localized repair. The complete web-suite result above predates this final repair; unchanged test evidence is retained.

Earlier computer-controlled checks used a real isolated API/database with scripted providers and explicitly synthetic evaluation cases. They verified independent keyboard disclosures, edited profile values across collapse/reopen, run creation and cancellation, populated evaluation evidence, audit evidence context, report switching with retained filter drafts, and compact desktop run context. The original mobile check identified epic execution-discovery overflow; its layout repair has unit coverage but still needs confirmation in the built browser.

The remaining visual acceptance work is a final built-candidate walkthrough of all page types, epic views and run sections, including narrow screens and coupled tab/execution-receipt transitions. App startup was restored after the PC restart, and the API, PostgreSQL and worker health were verified. Computer control currently exposes no browser, so these visual cases remain unproved. The owner requested a further simplify pass followed by a PR while this limitation remains visible.

The further simplify pass covered reuse, quality and efficiency. It established one efficiency improvement: hidden Usage views still started requests, including five-second subscription polling. Inactive reports now pause requests through the existing nullable-path API hook while remaining mounted, preserving filter drafts and pagination. Returning to a view refreshes its data, and All reports still loads every report. A focused regression first reproduced the unnecessary hidden requests, then all 26 affected tests passed after the repair. No additional compatible reuse or quality simplification was substantiated. This further audit reported truncated diff output; it supplements the earlier complete integrated review rather than replacing it.

One independent reviewer completed the integrated code review using the configured OpenAI fallback after the preferred Claude route failed authentication. Jev advisory review was partial because of hunk, context and operation limits. The complete diff remained available to the independent reviewer. Hosted Codex review is limited to three rounds, with outcomes tracked on the PR. Full hosted CI has not been dispatched and requires separate authorization for the specific run.
