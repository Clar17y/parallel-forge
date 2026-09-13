import { cleanup, render, screen } from '@testing-library/react';
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
  expect(fetcher.mock.calls).toHaveLength(1);
  await userEvent.click(screen.getByRole('button', { name: 'Inspect primary task task-1' }));
  await screen.findByText('Attempt 1 · running');
  expect(screen.getAllByText('Unknown').length).toBeGreaterThan(1);
  expect(screen.getByText('1 unsettled effect(s)')).toBeInTheDocument();
  expect(screen.getByText(/Effective:.*gpt-6-astra/)).toBeInTheDocument();
  expect(fetcher.mock.calls[1][0]).toContain('/subscription-tasks/task-1/attempts');
});

test('legacy runs retain their existing run view without a task inspector', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
    run_id: 'legacy', subscription: false, tasks: [], has_more: false,
  })));
  render(<TaskInspector runId="legacy" />);
  await screen.findByText('Legacy run: subscription tasks do not apply.');
  expect(screen.queryByRole('button', { name: /Inspect/ })).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Execution capacity')).not.toBeInTheDocument();
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
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
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
