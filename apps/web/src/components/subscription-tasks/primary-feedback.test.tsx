import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { csrf } from '@/lib/api/csrf';
import { PrimaryFeedback } from './primary-feedback';

const primaryTask = {
  task_id: 'primary-1', parent_task_id: null, dependency_task_ids: [],
  purpose: 'primary', owned_paths: [], state: 'implementing',
  pause_requested: false, cancel_requested: false, version: 5, repairs: 0,
  unsettled_effects: 0, control: null, feedback_receipts: [], primary_feedback_receipts: [],
  fallback_selected: false, recovery_attention: false,
};

const childTask = { ...primaryTask, task_id: 'task-1', parent_task_id: 'primary-1', purpose: 'routine_implementation' };

const receipt = {
  receipt_id: 'pf-1', operator_id: 'operator-1', run_id: 'run-1',
  primary_task_id: 'primary-1', task_id: 'primary-1', status: 'pending' as const,
  run_version: 3, task_version: 5,
  feedback_digest: 'a'.repeat(64), binding_digest: 'b'.repeat(64), feedback_bytes: 19,
  observed_at: '2026-10-21T12:00:00Z',
};

afterEach(() => { cleanup(); csrf.clear(); vi.restoreAllMocks(); });

test('renders primary feedback form for the root primary in a non-terminal state', () => {
  render(<PrimaryFeedback runId="run-1" runVersion={3} task={primaryTask} projectionToken={1} onRefresh={() => 2} />);
  expect(screen.getByText('Primary feedback')).toBeInTheDocument();
  expect(screen.getByLabelText('Feedback for primary')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Send primary feedback' })).toBeInTheDocument();
});

test('does not render for a child worker task', () => {
  const view = render(<PrimaryFeedback runId="run-1" runVersion={3} task={childTask} projectionToken={1} onRefresh={() => 2} />);
  expect(view.container).toBeEmptyDOMElement();
});

test('hides the form when the run is terminal', () => {
  render(<PrimaryFeedback runId="run-1" runVersion={3} runIsTerminal task={primaryTask} projectionToken={1} onRefresh={() => 2} />);
  expect(screen.queryByRole('button', { name: 'Send primary feedback' })).not.toBeInTheDocument();
  expect(screen.getByText('New primary feedback is unavailable for this run state.')).toBeInTheDocument();
});

test('submits primary feedback to the primary-feedback route and renders receipts', async () => {
  csrf.set('csrf-1');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(receipt)));
  const refresh = vi.fn(() => 2);
  render(<PrimaryFeedback runId="run-1" runVersion={3} task={{
    ...primaryTask,
    primary_feedback_receipts: [{
      receipt_id: 'pf-0', primary_task_id: 'primary-1', status: 'delivered' as const,
      feedback_digest: 'c'.repeat(64), feedback_bytes: 12, observed_at: '2026-10-21T11:00:00Z',
      delivery_attempt_id: 'attempt-9', delivered_at: '2026-10-21T11:05:00Z', closed_reason: null,
    }],
  }} projectionToken={1} onRefresh={refresh} />);
  expect(screen.getByText(/Delivered to primary/)).toBeInTheDocument();
  expect(screen.getByText(/attempt attempt-9/)).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText('Feedback for primary'), 'Stop blind repairs.');
  await userEvent.click(screen.getByRole('button', { name: 'Send primary feedback' }));
  await screen.findByText('Primary feedback recorded. It will reach the primary at its next attempt.');
  const [path, init] = fetcher.mock.calls[0];
  expect(path).toBe('/api/runs/run-1/subscription-tasks/primary-1/primary-feedback');
  expect(JSON.parse(String(init?.body))).toEqual({
    expected_run_version: 3, expected_task_version: 5, feedback: 'Stop blind repairs.',
  });
  expect(refresh).toHaveBeenCalledOnce();
});

test('shows a stale-binding recovery message on 409', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 409 }));
  const refresh = vi.fn(() => 7);
  render(<PrimaryFeedback runId="run-1" runVersion={3} task={primaryTask} projectionToken={5} onRefresh={refresh} />);
  await userEvent.type(screen.getByLabelText('Feedback for primary'), 'Use the proven root cause.');
  await userEvent.click(screen.getByRole('button', { name: 'Send primary feedback' }));
  await screen.findByText('This feedback was not accepted against the current task state. Review the refreshed receipts before sending it again.');
  expect(refresh).toHaveBeenCalledOnce();
});
