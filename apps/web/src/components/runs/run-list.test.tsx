import { render, screen } from '@testing-library/react';
import { expect, test } from 'vitest';
import { RunList } from './run-list';

test('run list preserves currencies and marks unpriced calls instead of inventing a total', () => {
  render(<RunList items={[{
    run_id: 'run-1', task_id: 'task-1', task_title: 'Build feature', project_id: 'project-1', project_name: 'Forge',
    state: 'IMPLEMENTING', version: 7, pending_gate: null, next_gate: 'pr', attention_required: false,
    local_remediation_count: 1, remote_remediation_count: 2, elapsed_ms: 90000, elapsed_seconds: 90,
    created_at: '2026-09-08T00:00:00Z', updated_at: '2026-09-08T00:01:30Z',
    pull_request: { repository: 'owner/repo', number: 3, head_sha: 'a'.repeat(40) },
    cost_summary: { currencies: [{ currency: 'USD', known_cost_minor: 14, unpriced_calls: 0 }, { currency: 'GBP', known_cost_minor: 7, unpriced_calls: 1 }], unpriced_calls: 1 },
  }]} />);
  expect(screen.getByRole('link', { name: 'Build feature' })).toHaveAttribute('href', '/runs/run-1');
  expect(screen.getByText('USD 14 minor units')).toBeInTheDocument();
  expect(screen.getByText('GBP 7 minor units')).toBeInTheDocument();
  expect(screen.getByText('1 unpriced calls')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '#3' })).toHaveAttribute('href', 'https://github.com/owner/repo/pull/3');
  const phaseCell = screen.getByText('Implementation phase').closest('td');
  expect(phaseCell).toBeInTheDocument();
  // List results are snapshots without polling, so they cannot assert live execution.
  expect(phaseCell?.querySelector('svg.animate-spin')).not.toBeInTheDocument();
});

test('remediation rows use the origin-neutral title because the list has no transition evidence', () => {
  render(<RunList items={[{
    run_id: 'run-2', task_id: 'task-2', task_title: 'Repair feature', project_id: 'project-1', project_name: 'Forge',
    state: 'REMEDIATING', version: 9, pending_gate: null, next_gate: null, attention_required: true,
    local_remediation_count: 1, remote_remediation_count: 1, elapsed_ms: 1000, elapsed_seconds: 1,
    created_at: '2026-09-08T00:00:00Z', updated_at: '2026-09-08T00:01:00Z',
    pull_request: null,
    cost_summary: { currencies: [], unpriced_calls: 0 },
  }]} />);
  expect(screen.getByText('Repairing recorded findings')).toBeInTheDocument();
  expect(screen.queryByText('Repairing local findings')).not.toBeInTheDocument();
});
