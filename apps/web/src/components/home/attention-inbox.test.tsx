import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { AttentionInbox } from './attention-inbox';
import { api } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });

const approval = {
  run_id: 'run-1', task_id: 'task-1', task_title: 'Add CSV export',
  project_id: 'project-1', project_name: 'Forge',
  gate: 'plan', evidence_digest: 'd'.repeat(64), run_version: 7, policy_version: 2,
};
const attentionRun = {
  run_id: 'run-2', task_id: 'task-2', task_title: 'Fix flaky checkout test',
  project_id: 'project-1', project_name: 'Forge', state: 'AWAITING_HUMAN_INTERVENTION',
  pending_gate: null, next_gate: null, attention_required: true,
  local_remediation_count: 0, remote_remediation_count: 0,
  created_at: '2026-10-10T09:00:00Z', updated_at: '2026-10-10T10:00:00Z',
  elapsed_ms: 60_000, elapsed_seconds: 60, pull_request: null,
  cost_summary: { currencies: [], unpriced_calls: 0 },
};

function mockInbox(approvals: () => unknown, runs: () => unknown) {
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path.startsWith('/approvals?')) return await approvals() as T;
    if (path.startsWith('/run-projections?')) return await runs() as T;
    throw new Error(`Unexpected API request: ${path}`);
  });
}

test('inbox shows who is waiting: approvals by task title, then other runs needing attention', async () => {
  mockInbox(
    () => ({ items: [approval], offset: 0, limit: 10, truncated: false }),
    // run-1 also needs attention (pending gate) but must not be listed twice.
    () => ({ items: [{ ...attentionRun, run_id: 'run-1' }, attentionRun], offset: 0, limit: 10, truncated: false }),
  );
  render(<AttentionInbox />);
  expect(await screen.findByRole('heading', { name: 'Waiting for your approval' })).toBeInTheDocument();
  expect(screen.getByText('Add CSV export')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Review plan evidence' })).toHaveAttribute('href', '/runs/run-1');
  expect(await screen.findByRole('heading', { name: 'Runs needing attention' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Fix flaky checkout test' })).toHaveAttribute('href', '/runs/run-2');
  expect(screen.getAllByText('Add CSV export')).toHaveLength(1);
  // Machine identifiers stay available, but demoted behind a disclosure.
  expect(screen.getByText('d'.repeat(64)).closest('details')).not.toBeNull();
});

test('inbox says so plainly when nothing is waiting', async () => {
  mockInbox(
    () => ({ items: [], offset: 0, limit: 10, truncated: false }),
    () => ({ items: [], offset: 0, limit: 10, truncated: false }),
  );
  render(<AttentionInbox />);
  expect(await screen.findByText('No approvals are waiting for you.')).toBeInTheDocument();
  expect(await screen.findByText('No runs need attention.')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Browse all runs' })).toHaveAttribute('href', '/runs');
});

test('a failed approvals load can be retried without hiding the runs queue', async () => {
  const loadApprovals = vi.fn().mockRejectedValueOnce(new Error('unavailable'))
    .mockResolvedValueOnce({ items: [approval], offset: 0, limit: 10, truncated: false });
  mockInbox(loadApprovals, () => ({ items: [attentionRun], offset: 0, limit: 10, truncated: false }));
  render(<AttentionInbox />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Approvals unavailable');
  expect(await screen.findByRole('link', { name: 'Fix flaky checkout test' })).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Retry approvals' }));
  expect(await screen.findByText('Add CSV export')).toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});
