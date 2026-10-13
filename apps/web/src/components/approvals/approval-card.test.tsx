import { render, screen } from '@testing-library/react';
import { expect, test } from 'vitest';
import { ApprovalCard } from './approval-card';

test('queue links to current cockpit evidence without granting approval from a stale list item', () => {
  render(<ApprovalCard item={{ run_id: 'run-1', task_id: 'task-1', task_title: 'Add CSV export', project_id: 'project-1', project_name: 'Forge', gate: 'plan', evidence_digest: 'd'.repeat(64), run_version: 7, policy_version: 2 }} />);
  expect(screen.getByText('Add CSV export')).toBeInTheDocument();
  expect(screen.getByText(/Forge/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Review plan evidence' })).toHaveAttribute('href', '/runs/run-1');
  expect(screen.getByText('d'.repeat(64))).toBeInTheDocument();
  expect(screen.getByText('d'.repeat(64)).closest('details')).not.toBeNull();
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});
