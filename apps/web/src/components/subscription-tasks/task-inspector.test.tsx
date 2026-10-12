import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { TaskInspector } from './task-inspector';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

test('loads attempts on selection and preserves unknown usage', async () => {
  const route = { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' };
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async input => new Response(JSON.stringify(
    String(input).includes('/attempts') ? {
      run_id: 'run-1', task_id: 'task-1', has_more: false, attempts: [{
        attempt_id: 'attempt-1', attempt_number: 1, state: 'running', requested_route: route, effective_route: route,
        input_tokens: null, output_tokens: null, cached_tokens: null, duration_ms: null,
        tool_calls: null, named_checks: null, estimated_api_cost_minor: null, currency: null, quota_status: 'unknown',
      }],
    } : { run_id: 'run-1', subscription: true, candidate_epoch: 2, candidate_state: 'open', has_more: false, tasks: [{
      task_id: 'task-1', parent_task_id: null, dependency_task_ids: [], purpose: 'primary', owned_paths: ['src'],
      state: 'leased', pause_requested: false, cancel_requested: false, version: 1, repairs: 0, unsettled_effects: 1,
    }] },
  ), { status: 200 }));
  render(<TaskInspector runId="run-1" />);
  await screen.findByText('src');
  expect(screen.getByText('Execution capacity: Unknown')).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/usage-checkpoint'))).toBe(true);
  await userEvent.click(screen.getByRole('button', { name: 'Inspect primary task task-1' }));
  await screen.findByText('Attempt 1 · running');
  expect(screen.getAllByText('Unknown').length).toBeGreaterThan(1);
  expect(screen.getByText('1 unsettled effect(s)')).toBeInTheDocument();
  expect(screen.getByText(/Effective:.*gpt-6-astra/)).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/subscription-tasks/task-1/attempts'))).toBe(true);
});

test('non-subscription runs retain the legacy message without subscription task controls', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
    run_id: 'legacy', subscription: false, tasks: [], has_more: false,
  })));
  render(<TaskInspector runId="legacy" />);
  await screen.findByText('Legacy run: subscription tasks do not apply.');
  expect(screen.queryByRole('button', { name: /Inspect/ })).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Execution capacity')).not.toBeInTheDocument();
});

test('ordinary non-subscription run shows measured checkpoint controls and can continue an owner checkpoint', async () => {
  const binding = {
    subject: { project_id: 'project-1', kind: 'run', subject_id: 'run-1', schema_version: 1 }, work_unit_id: 'unit-1',
    admitted_phase_id: 'implementation', configured_route: { fingerprint: 'a'.repeat(64), members: [] },
    effective_route: { fingerprint: 'b'.repeat(64), members: [] }, source: { kind: 'agent_execution', source_id: 'source-1', parent_id: null }, schema_version: 1,
  };
  const checkpoint = { checkpoint_id: 'checkpoint-1', binding, baseline_binding: binding, state: 'paused', policy_revision: 1,
    reference_digest: 'c'.repeat(64), trigger_event_id: 'trigger-1', version: 2, generation: 1, process_settled: true,
    next_checkpoint_threshold_tokens: null, usage_pauses_disabled: false, monitoring_policy_revision: 1 };
  const baseline = { binding, policy_revision: 1, snapshot_digest: 'd'.repeat(64), cohort_digest: 'e'.repeat(64), history_digest: 'f'.repeat(64),
    estimator_version: 'v1', comparison_scope: 'route', completeness: 'complete', coverage_count: 4, sample_count: 4, outcome_count: 4,
    source_count: 4, window_limit: 20, minimum_comparables: 3, status: 'ready', reference_tokens: 100, mean_tokens: 100,
    median_tokens: 100, p90_tokens: 120, warning_threshold_tokens: 150, checkpoint_threshold_tokens: 200, frozen_at: '2026-10-11T10:00:00Z', schema_version: 1 };
  const report = { binding, policy_revision: 1, baseline, input_tokens: 25, output_tokens: 30, cached_input_tokens: null,
    reasoning_output_tokens: null, context_occupied_tokens: null, context_peak_tokens: null, context_capacity_tokens: null,
    compaction_count: null, completeness: 'complete', freshness: 'final', sources: [] };
  const response = { checkpoint, report };
  const page = { run_id: 'run-1', subscription: false, tasks: [], has_more: false, run_allows_execution: true, run_is_terminal: false,
    quota_statuses: [{ provider: 'claude', account: 'personal', pool: 'daily', status: 'blocked', revision: 2, observed_at: null,
      reason: 'limit_reached', reset_at: null, next_eligible_at: null, retry_basis: null, probe_attempt_id: null, recovered_at: null }] };
  const user = userEvent.setup();
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if ((init?.method ?? 'GET') === 'POST') return new Response(JSON.stringify({ receipt: { checkpoint_id: 'checkpoint-1', action: 'continue', state: 'resumed', result_version: 3 }, checkpoint: { ...checkpoint, state: 'resumed', version: 3 }, report }), { status: 200 });
    return new Response(JSON.stringify(String(input).includes('/usage-checkpoint') ? response : page), { status: 200 });
  });
  render(<TaskInspector runId="run-1" />);
  expect(await screen.findByText('Legacy run: subscription tasks do not apply.')).toBeInTheDocument();
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });
  expect(region).toHaveTextContent('Input tokens: 25');
  expect(region).not.toHaveTextContent(/subscription task on this page is waiting for provider quota/i);
  await user.click(within(region).getByRole('button', { name: 'Continue to next checkpoint' }));
  expect(await screen.findByText(/Continue recorded/)).toBeVisible();
});

test('shows observed execution capacity and the reason a selected route is queued', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
    run_id: 'run-1', subscription: true, tasks: [{
      task_id: 'task-1', parent_task_id: 'primary', dependency_task_ids: [], purpose: 'routine_implementation', owned_paths: ['alpha/value.txt'],
      state: 'queued', pause_requested: false, cancel_requested: false, version: 1, repairs: 0, unsettled_effects: 0,
      capacity_waits: ['host'],
    }], has_more: false, quota_statuses: [],
    capacity: { observed_at: '2026-09-12T12:00:00Z', policy_version: 2,
      host: { active: 2, limit: 2 }, run: { active: 1, limit: 3 },
      providers: [{ provider: 'google', active: 1, limit: 2 }],
      queue_order: 'least_recently_served_run_then_oldest_task' },
  })));
  render(<TaskInspector runId="run-1" />);
  await screen.findByText('Host: 2/2 · This run: 1/3');
  expect(screen.getByText('google: 1/2')).toBeInTheDocument();
  expect(screen.getByText('Capacity limits reached: host')).toBeInTheDocument();
  expect(screen.getByText(/Least recently served run, then oldest task/)).toBeInTheDocument();
  expect(screen.queryByText(/remaining allowance/i)).not.toBeInTheDocument();
});

test('shows task quota reason and retry time beyond the global status page', async () => {
  const route = { provider: 'anthropic', client: 'claude_code', model: 'opus', effort: 'medium', auth_mode: 'subscription', billing_mode: 'allowance_only' };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => new Response(JSON.stringify(String(input).includes('/usage-checkpoint')
    ? { checkpoint: null, report: null } : {
    run_id: 'run-1', subscription: true, tasks: [{
      task_id: 'task-1', parent_task_id: 'primary', dependency_task_ids: [], purpose: 'routine_implementation', owned_paths: ['src'],
      state: 'queued', pause_requested: false, cancel_requested: false, version: 1, repairs: 0, unsettled_effects: 0,
      effective_route: route, fallback_selected: true,
      quota_status: { provider: 'anthropic', account: 'team-a', pool: 'weekly', status: 'blocked', revision: 1,
        observed_at: '2026-09-12T12:00:00Z', reason: 'operator_report', reset_at: null,
        next_eligible_at: '2026-09-12T13:00:00Z', retry_basis: 'probe_cooldown', probe_attempt_id: null, recovered_at: null },
    }], has_more: false, quota_statuses: [],
  })));
  render(<TaskInspector runId="run-1" />);
  await screen.findByText('Reason: operator_report');
  expect(await screen.findByText('A subscription task on this page is waiting for provider quota eligibility. Recording a monitoring choice does not clear the quota wait.')).toBeInTheDocument();
  expect(screen.getByText(/Next probe:/)).toBeInTheDocument();
  expect(screen.getByText(/approved fallback selected/)).toBeInTheDocument();
});

test('the inspector resumes the visible settled pause with versions from the same snapshot', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (init?.method === 'POST') return new Response(JSON.stringify({
      receipt_id: 'resume-1', run_id: 'run-1', task_id: 'task-1', action: 'resume',
      status: 'decision_pending', run_version: 7, task_version: 5,
      reason: 'Continue', observed_at: '2026-09-12T06:00:00Z', pause_receipt_id: 'pause-1',
    }));
    if (String(input).includes('/attempts')) return new Response(JSON.stringify({
      run_id: 'run-1', task_id: 'task-1', attempts: [], has_more: false,
    }));
    return new Response(JSON.stringify({
      run_id: 'run-1', run_version: 7, run_allows_execution: true, subscription: true,
      tasks: [{ task_id: 'task-1', parent_task_id: 'primary', dependency_task_ids: [],
        purpose: 'routine_implementation', owned_paths: ['src'], state: 'reconciling',
        pause_requested: true, cancel_requested: false, version: 4, repairs: 0, unsettled_effects: 0,
        control: { receipt_id: 'pause-1', action: 'pause', status: 'paused', reason: 'Inspect work',
          observed_at: '2026-09-12T05:00:00Z', pause_receipt_id: 'pause-1' } }],
      has_more: false, quota_statuses: [],
    }));
  });
  render(<TaskInspector runId="run-1" />);
  await screen.findByText('Paused');
  expect(screen.queryByText('Pause requested')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Inspect routine_implementation task task-1' }));
  await userEvent.type(screen.getByLabelText('Task control reason'), 'Continue');
  await userEvent.click(screen.getByRole('button', { name: 'Resume task' }));
  await screen.findByText('Resume recorded. The retained decision is ready for guarded application.');
  const mutation = fetcher.mock.calls.find(([, init]) => init?.method === 'POST');
  expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({
    action: 'resume', expected_run_version: 7, expected_task_version: 4, reason: 'Continue', pause_receipt_id: 'pause-1',
  });
});

test('the selected specialist shows its retained feedback receipt and bounded feedback control', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => new Response(JSON.stringify(
    String(input).includes('/attempts') ? {
      run_id: 'run-1', task_id: 'worker-1', attempts: [], has_more: false,
    } : {
      run_id: 'run-1', run_version: 8, run_allows_execution: true, subscription: true,
      tasks: [{
        task_id: 'worker-1', parent_task_id: 'primary-1', dependency_task_ids: [],
        purpose: 'routine_implementation', owned_paths: ['src/parser.ts'], state: 'leased',
        pause_requested: false, cancel_requested: false, version: 4, repairs: 0,
        unsettled_effects: 0, feedback_receipts: [{
          receipt_id: 'feedback-1', primary_task_id: 'primary-1', status: 'forwarded',
          feedback_digest: 'a'.repeat(64), feedback_bytes: 38,
          observed_at: '2026-09-18T12:00:00Z', closed_reason: null,
        }],
      }],
      has_more: false, quota_statuses: [],
    },
  )));
  render(<TaskInspector runId="run-1" />);
  await screen.findByText('src/parser.ts');
  await userEvent.click(screen.getByRole('button', { name: 'Inspect routine_implementation task worker-1' }));

  const feedback = screen.getByRole('region', { name: 'Worker feedback' });
  expect(feedback).toHaveTextContent('Target worker: routine_implementation task worker-1');
  expect(feedback).toHaveTextContent('Forwarded to worker');
  expect(feedback).toHaveTextContent('Receipt feedback-1');
  expect(feedback).toHaveTextContent('does not issue provider commands');
});

test('shows attention for primary and child task and navigates to the affected attempt', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => new Response(JSON.stringify(
    String(input).includes('/attempts') ? { run_id: 'run-1', task_id: 'primary-1', attempts: [{
      attempt_id: 'attempt-1', attempt_number: 2, state: 'decision_pending', requested_route: { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' },
      effective_route: { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' },
      input_tokens: null, output_tokens: null, cached_tokens: null, duration_ms: null, tool_calls: null, named_checks: null,
      estimated_api_cost_minor: null, currency: null, quota_status: 'unknown',
      recovery: { classification: 'unsupported_application', reason_code: 'invalid_primary_handoff', resolution: 'attention_required', failed_applications: 2, first_failure_at: '2026-09-28T10:00:00Z', last_failure_at: '2026-09-28T10:01:00Z', next_retry_at: '2026-09-28T10:02:00Z', eligible_actions: [] },
    }], has_more: false } : { run_id: 'run-1', subscription: true, recovery_attention: true, tasks: [
      { task_id: 'primary-1', parent_task_id: null, dependency_task_ids: [], purpose: 'primary', owned_paths: [], state: 'decision_pending', recovery_attention: true, recovery_reason_code: 'invalid_primary_handoff', pause_requested: false, cancel_requested: false, version: 1, repairs: 0, unsettled_effects: 0 },
      { task_id: 'child-1', parent_task_id: 'primary-1', dependency_task_ids: [], purpose: 'routine_implementation', owned_paths: ['src'], state: 'blocked', recovery_attention: true, recovery_reason_code: 'application_failed', pause_requested: false, cancel_requested: false, version: 1, repairs: 0, unsettled_effects: 0 },
    ], has_more: false, run_version: 2, run_allows_execution: true, run_is_terminal: false, quota_statuses: [] },
  ), { status: 200 }));
  render(<TaskInspector runId="run-1" />);
  await screen.findByText(/Recovery attention required/);
  expect(screen.getByText(/Primary needs recovery/)).toBeInTheDocument();
  expect(screen.getByText(/Task needs recovery/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Recover task' })).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Recover run' }));
  expect(await screen.findByText(/Last failure: .*invalid_primary_handoff/)).toBeInTheDocument();
  expect(screen.getByText(/Next retry: 2026-09-28T10:02:00Z/)).toBeInTheDocument();
  expect(screen.getAllByText(/last completed tool/).length).toBeGreaterThan(0);
});
