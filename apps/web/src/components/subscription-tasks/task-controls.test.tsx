import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { csrf } from '@/lib/api/csrf';
import { TaskControls } from './task-controls';

const task = {
  task_id: 'task-1', parent_task_id: 'primary', dependency_task_ids: [],
  purpose: 'routine_implementation', owned_paths: ['src'], state: 'leased',
  pause_requested: false, cancel_requested: false, version: 4, repairs: 0, unsettled_effects: 0,
  control: null, fallback_selected: false,
};
const pause = {
  receipt_id: 'pause-1', action: 'pause' as const, status: 'paused' as const,
  observed_at: '2026-09-12T06:00:00Z', reason: 'Check ownership',
  pause_receipt_id: 'pause-1',
};
const receipt = (action: string) => ({
  receipt_id: 'receipt-1', run_id: 'run-1', task_id: 'task-1', action,
  status: action === 'resume' ? 'decision_pending' : `${action}_requested`,
  run_version: 3, task_version: 5, observed_at: '2026-09-12T06:00:00Z',
  reason: 'Check ownership', pause_receipt_id: action === 'resume' ? 'pause-1' : null,
});

afterEach(() => { cleanup(); csrf.clear(); vi.restoreAllMocks(); });

test('sends the visible versions, an audit reason and CSRF with a fresh idempotency key', async () => {
  csrf.set('csrf-1');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(receipt('pause'))));
  const refresh = vi.fn(() => 2);
  const view = render(<TaskControls runId="run-1" runVersion={3} runAllowsExecution task={task} projectionToken={1} onRefresh={refresh} />);
  const reasonField = screen.getByLabelText('Task control reason');
  await userEvent.type(reasonField, 'Check ownership');
  await userEvent.click(screen.getByRole('button', { name: 'Pause task' }));
  await screen.findByText('Pause requested. Waiting for stopped work to be confirmed.');
  expect(fetcher).toHaveBeenCalledTimes(1);
  const [path, init] = fetcher.mock.calls[0];
  expect(path).toBe('/api/runs/run-1/subscription-tasks/task-1/controls');
  expect(JSON.parse(String(init?.body))).toEqual({ action: 'pause', expected_run_version: 3,
    expected_task_version: 4, reason: 'Check ownership', pause_receipt_id: null });
  expect(new Headers(init?.headers).get('Idempotency-Key')).toMatch(/^[a-f0-9-]{36}$/);
  expect(new Headers(init?.headers).get('X-CSRF-Token')).toBe('csrf-1');
  expect(refresh).toHaveBeenCalledOnce();
  expect(reasonField).toHaveAttribute('readonly');

  view.rerender(<TaskControls runId="run-1" runVersion={3} runAllowsExecution task={task} projectionToken={2} onRefresh={refresh} />);
  expect(reasonField).not.toHaveAttribute('readonly');
});

test('an unconfirmed response retries the identical body and key after another tab changes versions', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce(new TypeError('offline'))
    .mockResolvedValueOnce(new Response(JSON.stringify(receipt('pause'))));
  const refresh = vi.fn();
  const view = render(<TaskControls runId="run-1" runVersion={3} runAllowsExecution task={task} projectionToken={0} onRefresh={refresh} />);
  await userEvent.type(screen.getByLabelText('Task control reason'), 'Check ownership');
  await userEvent.click(screen.getByRole('button', { name: 'Pause task' }));
  await screen.findByRole('button', { name: 'Retry same request' });
  view.rerender(<TaskControls runId="run-1" runVersion={9} runAllowsExecution task={{ ...task, version: 8 }} projectionToken={0} onRefresh={refresh} />);
  expect(screen.getByLabelText('Task control reason')).toHaveAttribute('readonly');
  await userEvent.click(screen.getByRole('button', { name: 'Retry same request' }));
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
  const first = fetcher.mock.calls[0][1], second = fetcher.mock.calls[1][1];
  expect(second?.body).toBe(first?.body);
  expect(new Headers(second?.headers).get('Idempotency-Key')).toBe(new Headers(first?.headers).get('Idempotency-Key'));
});

test('a version conflict refreshes state and requires a new deliberate request', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response('{}', { status: 409 }))
    .mockResolvedValueOnce(new Response(JSON.stringify(receipt('cancel'))));
  const refresh = vi.fn(() => 7);
  const view = render(<TaskControls runId="run-1" runVersion={3} runAllowsExecution task={task} projectionToken={5} onRefresh={refresh} />);
  const reasonField = screen.getByLabelText('Task control reason');
  await userEvent.type(reasonField, 'Check ownership');
  await userEvent.click(screen.getByRole('button', { name: 'Cancel task' }));
  await screen.findByText('The task or run changed. Review the refreshed state before acting again.');
  expect(refresh).toHaveBeenCalledOnce();
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('button', { name: 'Retry same request' })).not.toBeInTheDocument();

  // The panel remains fenced before revision advancement
  expect(reasonField).toHaveValue('');
  expect(reasonField).toHaveAttribute('readonly');
  expect(screen.getByRole('button', { name: 'Cancel task' })).toBeDisabled();

  // An older response cannot unlock the target refresh fence.
  view.rerender(<TaskControls runId="run-1" runVersion={3} runAllowsExecution task={task} projectionToken={6} onRefresh={refresh} />);
  expect(reasonField).toHaveAttribute('readonly');
  await userEvent.click(screen.getByRole('button', { name: 'Cancel task' }));
  expect(fetcher).toHaveBeenCalledOnce();

  // Exact target token unlocks controls with unchanged run/task versions.
  view.rerender(<TaskControls runId="run-1" runVersion={3} runAllowsExecution task={task} projectionToken={7} onRefresh={refresh} />);
  expect(reasonField).not.toHaveAttribute('readonly');

  await userEvent.type(reasonField, 'Deliberate cancel after refresh');
  expect(screen.getByRole('button', { name: 'Cancel task' })).toBeEnabled();
  await userEvent.click(screen.getByRole('button', { name: 'Cancel task' }));
  await screen.findByText('Cancellation requested. Waiting for stopped work to be confirmed.');
  expect(fetcher).toHaveBeenCalledTimes(2);
});

test('resumes from a settled causal pause and shows its audited reason', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(receipt('resume'))));
  render(<TaskControls runId="run-1" runVersion={3} runAllowsExecution
    task={{ ...task, state: 'reconciling', pause_requested: true, control: pause }} projectionToken={0} onRefresh={vi.fn()} />);
  expect(screen.getByText('Paused')).toBeInTheDocument();
  expect(screen.getByText('Reason: Check ownership')).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText('Task control reason'), 'Continue within the approved scope');
  await userEvent.click(screen.getByRole('button', { name: 'Resume task' }));
  await screen.findByText('Resume recorded. The retained decision is ready for guarded application.');
  expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body)).pause_receipt_id).toBe('pause-1');
});

test('resuming a waiting task preserves its pending decision status', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ...receipt('resume'), status: 'blocked' })));
  const refresh = vi.fn();
  render(<TaskControls runId="run-1" runVersion={3} runAllowsExecution
    task={{ ...task, state: 'blocked', pause_requested: true, control: pause }} projectionToken={0} onRefresh={refresh} />);
  await userEvent.type(screen.getByLabelText('Task control reason'), 'Continue waiting for scope approval');
  await userEvent.click(screen.getByRole('button', { name: 'Resume task' }));
  await screen.findByText('Resume recorded. The task is still waiting for its pending decision.');
  expect(refresh).toHaveBeenCalledOnce();
  expect(screen.queryByRole('button', { name: 'Retry same request' })).not.toBeInTheDocument();
});

test('unconfirmed pauses cannot resume, and run stops and primary tasks preserve run controls', () => {
  const view = render(<TaskControls runId="run-1" runVersion={3} runAllowsExecution
    task={{ ...task, pause_requested: true }} projectionToken={0} onRefresh={vi.fn()} />);
  expect(screen.queryByRole('button', { name: 'Resume task' })).not.toBeInTheDocument();
  view.rerender(<TaskControls runId="run-1" runVersion={3} runAllowsExecution={false}
    task={{ ...task, pause_requested: true, control: pause }} projectionToken={0} onRefresh={vi.fn()} />);
  expect(screen.queryByRole('button', { name: 'Resume task' })).not.toBeInTheDocument();
  view.rerender(<TaskControls runId="run-1" runVersion={3} runAllowsExecution={false} runIsTerminal task={task} projectionToken={0} onRefresh={vi.fn()} />);
  expect(screen.queryByRole('button', { name: 'Cancel task' })).not.toBeInTheDocument();
  view.rerender(<TaskControls runId="run-1" runVersion={3} runAllowsExecution task={{ ...task, purpose: 'primary' }} projectionToken={0} onRefresh={vi.fn()} />);
  expect(screen.getByText('Use run controls for the primary coordinator.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Cancel task' })).not.toBeInTheDocument();
});

test('rejects a blank or overlong UTF-8 reason and prevents duplicate clicks', async () => {
  let finish!: (response: Response) => void;
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  render(<TaskControls runId="run-1" runVersion={3} runAllowsExecution task={task} projectionToken={0} onRefresh={vi.fn()} />);
  expect(screen.getByRole('button', { name: 'Pause task' })).toBeDisabled();
  await userEvent.type(screen.getByLabelText('Task control reason'), '界'.repeat(171));
  expect(screen.getByRole('button', { name: 'Pause task' })).toBeDisabled();
  await userEvent.clear(screen.getByLabelText('Task control reason'));
  await userEvent.type(screen.getByLabelText('Task control reason'), 'Check ownership');
  await userEvent.dblClick(screen.getByRole('button', { name: 'Pause task' }));
  expect(fetcher).toHaveBeenCalledTimes(1);
  finish(new Response(JSON.stringify(receipt('pause'))));
  await screen.findByText('Pause requested. Waiting for stopped work to be confirmed.');
});
