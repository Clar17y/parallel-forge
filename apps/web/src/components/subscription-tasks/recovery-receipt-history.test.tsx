import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { RecoveryReceiptHistory } from './recovery-receipt-history';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const receipt = (id: string) => ({ receipt_id: id, run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', action: 'retry_application', status: 'applied', observed_at: '2026-09-29T12:00:00Z', reason_code: 'applied' });

test('shows paginated durable receipts after a new session without mutation', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const offset = Number(new URL(String(input), 'http://localhost').searchParams.get('offset'));
    return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipts: [receipt(offset ? 'second-receipt' : 'first-receipt')], has_more: !offset }));
  });
  render(<RecoveryReceiptHistory runId="run-1" taskId="task-1" attemptId="attempt-1" />);
  expect(await screen.findByText(/first-receipt/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Next receipts' }));
  expect(await screen.findByText(/second-receipt/)).toBeInTheDocument();
  expect(screen.queryByText(/first-receipt/)).not.toBeInTheDocument();
  expect(fetcher.mock.calls.map(([url]) => String(url))).toEqual([
    '/api/runs/run-1/subscription-tasks/task-1/attempts/attempt-1/recovery/receipts?offset=0&limit=25',
    '/api/runs/run-1/subscription-tasks/task-1/attempts/attempt-1/recovery/receipts?offset=25&limit=25',
  ]);
  expect(fetcher.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true);
});

test('empty history does not claim an uncertain request never committed', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipts: [], has_more: false })));
  render(<RecoveryReceiptHistory runId="run-1" taskId="task-1" attemptId="attempt-1" />);
  expect(await screen.findByText(/does not rule out a request still in progress/i)).toBeInTheDocument();
});
