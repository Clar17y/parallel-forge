import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { csrf } from '@/lib/api/csrf';
import { TaskFeedback } from './task-feedback';

const task = {
  task_id: 'task-1', parent_task_id: 'primary-1', dependency_task_ids: [],
  purpose: 'routine_implementation', owned_paths: ['src/parser.ts'], state: 'leased',
  pause_requested: false, cancel_requested: false, version: 4, repairs: 0,
  unsettled_effects: 0, control: null, feedback_receipts: [], fallback_selected: false,
};

const receipt = {
  receipt_id: 'feedback-1', operator_id: 'operator-1', run_id: 'run-1',
  primary_task_id: 'primary-1', task_id: 'task-1', status: 'pending_primary' as const,
  run_version: 3, task_version: 4, primary_task_version: 7,
  feedback_digest: 'a'.repeat(64), binding_digest: 'b'.repeat(64), feedback_bytes: 37,
  observed_at: '2026-09-18T12:00:00Z',
};

afterEach(() => { cleanup(); csrf.clear(); vi.restoreAllMocks(); });

test('submits feedback for the displayed worker with exact versions and a durable receipt', async () => {
  csrf.set('csrf-1');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(JSON.stringify(receipt)),
  );
  const refresh = vi.fn(() => 2);
  const view = render(<TaskFeedback runId="run-1" runVersion={3} task={task} projectionToken={1} onRefresh={refresh} />);

  expect(screen.getByText('Target worker: routine_implementation task task-1')).toBeInTheDocument();
  const field = screen.getByLabelText('Feedback for routine_implementation worker');
  await userEvent.type(field, 'Keep the parser; add replay coverage.');
  await userEvent.click(screen.getByRole('button', { name: 'Send worker feedback' }));

  await screen.findByText('Feedback recorded. The primary coordinator will forward its retained receipt.');
  expect(fetcher).toHaveBeenCalledOnce();
  const [path, init] = fetcher.mock.calls[0];
  expect(path).toBe('/api/runs/run-1/subscription-tasks/task-1/feedback');
  expect(JSON.parse(String(init?.body))).toEqual({
    expected_run_version: 3,
    expected_task_version: 4,
    feedback: 'Keep the parser; add replay coverage.',
  });
  expect(new Headers(init?.headers).get('Idempotency-Key')).toMatch(/^[a-f0-9-]{36}$/);
  expect(new Headers(init?.headers).get('X-CSRF-Token')).toBe('csrf-1');
  expect(refresh).toHaveBeenCalledOnce();
  expect(field).toHaveAttribute('readonly');

  view.rerender(<TaskFeedback runId="run-1" runVersion={3} task={task} projectionToken={2} onRefresh={refresh} />);
  expect(field).not.toHaveAttribute('readonly');
});

test('an unconfirmed response retries the identical feedback body and key after versions change', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce(new TypeError('offline'))
    .mockResolvedValueOnce(new Response(JSON.stringify(receipt)));
  const refresh = vi.fn();
  const view = render(<TaskFeedback runId="run-1" runVersion={3} task={task} projectionToken={0} onRefresh={refresh} />);
  await userEvent.type(screen.getByLabelText('Feedback for routine_implementation worker'), 'Keep the parser; add replay coverage.');
  await userEvent.click(screen.getByRole('button', { name: 'Send worker feedback' }));
  await screen.findByRole('button', { name: 'Retry same request' });

  view.rerender(<TaskFeedback runId="run-1" runVersion={9} task={{ ...task, version: 8 }} projectionToken={0} onRefresh={refresh} />);
  expect(screen.getByLabelText('Feedback for routine_implementation worker')).toHaveAttribute('readonly');
  await userEvent.click(screen.getByRole('button', { name: 'Retry same request' }));
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());

  const first = fetcher.mock.calls[0][1];
  const second = fetcher.mock.calls[1][1];
  expect(second?.body).toBe(first?.body);
  expect(new Headers(second?.headers).get('Idempotency-Key'))
    .toBe(new Headers(first?.headers).get('Idempotency-Key'));
});

test('an older projection token cannot unlock a 409 fence, while its exact target can', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response('{}', { status: 409 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...receipt, feedback_bytes: 46 })));
  const refresh = vi.fn(() => 7);
  const view = render(<TaskFeedback runId="run-1" runVersion={3} task={task} projectionToken={5} onRefresh={refresh} />);
  const field = screen.getByLabelText('Feedback for routine_implementation worker');
  await userEvent.type(field, 'Review the refreshed worker before continuing.');
  await userEvent.click(screen.getByRole('button', { name: 'Send worker feedback' }));

  await screen.findByText(
    'This feedback was not accepted against the current task state. Review the refreshed receipts before sending it again.',
  );
  expect(refresh).toHaveBeenCalledOnce();
  expect(fetcher).toHaveBeenCalledOnce();

  // The form must remain fenced while projection revision is unchanged
  const button = screen.getByRole('button', { name: 'Send worker feedback' });
  expect(button).toBeDisabled();
  expect(field).toHaveAttribute('readonly');

  // Direct click attempt cannot POST before revision advances
  await userEvent.click(button);
  expect(fetcher).toHaveBeenCalledOnce();

  // A stale completed poll must not unlock the fence, even with identical data.
  view.rerender(<TaskFeedback runId="run-1" runVersion={3} task={task} projectionToken={6} onRefresh={refresh} />);
  expect(button).toBeDisabled();
  await userEvent.click(button);
  expect(fetcher).toHaveBeenCalledOnce();

  // The exact refresh target unlocks with unchanged run/task versions and data.
  view.rerender(<TaskFeedback runId="run-1" runVersion={3} task={task} projectionToken={7} onRefresh={refresh} />);
  expect(button).toBeEnabled();
  expect(field).not.toHaveAttribute('readonly');

  await userEvent.click(button);
  await screen.findByText('Feedback recorded. The primary coordinator will forward its retained receipt.');
  expect(fetcher).toHaveBeenCalledTimes(2);
});


test('renders retained lifecycle state without exposing feedback text', () => {
  const sensitiveMarker = 'Keep the retained parser bytes and add replay coverage.';
  render(<TaskFeedback runId="run-1" runVersion={3} task={{
    ...task,
    feedback_receipts: [{
      receipt_id: 'feedback-1', primary_task_id: 'primary-1', status: 'closed' as const,
      feedback_digest: 'c'.repeat(64), feedback_bytes: 28,
      observed_at: '2026-09-18T12:00:00Z', closed_reason: 'budget_exhausted' as const,
      ...({ feedback: sensitiveMarker, raw_feedback: sensitiveMarker }),
    }],
  }} projectionToken={0} onRefresh={vi.fn()} />);

  const history = screen.getByRole('list', { name: 'Retained feedback receipts' });
  expect(history).toHaveTextContent('Closed · task budget exhausted');
  expect(history).toHaveTextContent(`digest ${'c'.repeat(64)}`);
  expect(history).not.toHaveTextContent(sensitiveMarker);
});

test('keeps primary control at run level and disables new feedback for terminal targets', () => {
  const view = render(<TaskFeedback runId="run-1" runVersion={3}
    task={{ ...task, purpose: 'primary', parent_task_id: null }} projectionToken={0} onRefresh={vi.fn()} />);
  expect(screen.queryByRole('region', { name: 'Worker feedback' })).not.toBeInTheDocument();

  view.rerender(<TaskFeedback runId="run-1" runVersion={3} runIsTerminal task={task} projectionToken={0} onRefresh={vi.fn()} />);
  expect(screen.getByText('New feedback is unavailable for this task state.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Send worker feedback' })).not.toBeInTheDocument();
});

test('enforces the UTF-8 byte bound and prevents duplicate submissions', async () => {
  let finish!: (response: Response) => void;
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(
    () => new Promise(resolve => { finish = resolve; }),
  );
  render(<TaskFeedback runId="run-1" runVersion={3} task={task} projectionToken={0} onRefresh={vi.fn()} />);
  const field = screen.getByLabelText('Feedback for routine_implementation worker');
  fireEvent.change(field, { target: { value: '界'.repeat(1366) } });
  expect(screen.getByRole('button', { name: 'Send worker feedback' })).toBeDisabled();
  await userEvent.clear(field);
  await userEvent.type(field, 'Keep the parser; add replay coverage.');
  await userEvent.dblClick(screen.getByRole('button', { name: 'Send worker feedback' }));
  expect(fetcher).toHaveBeenCalledOnce();
  finish(new Response(JSON.stringify(receipt)));
  await screen.findByText('Feedback recorded. The primary coordinator will forward its retained receipt.');
});
