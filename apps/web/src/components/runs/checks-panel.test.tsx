import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { api } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';
import { projection } from '@/test/projection';
import { ChecksPanel } from './checks-panel';
import { ChecksSummary } from './checks-summary';

vi.mock('@/lib/api/client', async original => ({ ...await original<typeof import('@/lib/api/client')>(), api: vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });

function check(overrides: Partial<components['schemas']['CheckHistoryItem']> = {}): components['schemas']['CheckHistoryItem'] {
  return {
    id: 'check-1', name: 'test-web', command_name: 'test-web', command_version: 6,
    status: 'FAILED', exit_code: 1, output_artifact_digest: 'stdout', stderr_artifact_digest: 'stderr',
    completed_at: '2026-10-19T10:01:00Z', head_sha: 'a'.repeat(40), step_id: null,
    attempt: 2, started_at: '2026-10-19T10:00:00Z', duration_ms: 60000,
    evidence_digest: 'receipt', configured_runner_mode: 'docker', source: 'subscription',
    subscription_task_id: 'task-1', subscription_attempt_id: 'attempt-2', ...overrides,
  };
}

test('history exposes subscription attempt and exact receipt/output links alongside running work', async () => {
  vi.mocked(api).mockResolvedValue({ items: [check(), check({ id: 'check-2', attempt: 3,
    subscription_attempt_id: 'attempt-3', status: 'RUNNING', exit_code: null, completed_at: null,
    duration_ms: null, evidence_digest: null, output_artifact_digest: null, stderr_artifact_digest: null,
  })], truncated: false });
  render(<ChecksPanel projection={projection()} />);
  await screen.findByText('attempt-3');
  expect(screen.getByText('attempt-2')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Check receipt for attempt 2' })).toHaveAttribute('href', '/api/artifacts/receipt/download');
  expect(screen.getByRole('link', { name: 'Bounded output for test-web' })).toHaveAttribute('href', '/api/artifacts/stdout/download');
  expect(screen.getByRole('link', { name: 'Standard error for test-web' })).toHaveAttribute('href', '/api/artifacts/stderr/download');
  expect(screen.queryByText('No local validation checks recorded.')).not.toBeInTheDocument();
  expect(screen.getByText(/Attempt 3 · Duration: Pending/)).toBeInTheDocument();
});

test('an interrupted completed check has unknown duration, not pending or passing', async () => {
  vi.mocked(api).mockResolvedValue({ items: [check({ status: 'UNKNOWN', exit_code: null,
    duration_ms: null, evidence_digest: null, output_artifact_digest: null, stderr_artifact_digest: null,
  })], truncated: false });
  render(<ChecksPanel projection={projection()} />);
  expect(await screen.findByText(/Duration: Unknown/)).toBeInTheDocument();
  expect(screen.queryByRole('link', { name: /Check receipt/ })).not.toBeInTheDocument();
});

test('overview shows running and unknown checks without a candidate and never approves a different head', () => {
  const p = projection(); p.candidate.commit = null;
  p.checks = [check({ id: 'running', status: 'RUNNING', exit_code: null }),
    check({ id: 'unknown', status: 'UNKNOWN', exit_code: null }), check({ id: 'passed', status: 'SUCCEEDED', exit_code: 0 })];
  const view = render(<ChecksSummary projection={p} stale={false} onInspect={() => {}} />);
  const summary = within(screen.getByRole('region', { name: 'Checks at a glance' }));
  expect(summary.getByText('Running')).toBeInTheDocument();
  expect(summary.getByText('Outcome unknown')).toBeInTheDocument();
  expect(summary.queryByText('Passed', { exact: true })).not.toBeInTheDocument();
  p.candidate.commit = 'b'.repeat(40);
  view.rerender(<ChecksSummary projection={p} stale={false} onInspect={() => {}} />);
  expect(summary.getByText('Different head')).toBeInTheDocument();
  expect(summary.queryByText('Passed', { exact: true })).not.toBeInTheDocument();
});
