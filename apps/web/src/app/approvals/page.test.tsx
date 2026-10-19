import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import ApprovalsPage from './page';
import { api } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.mocked(api).mockReset();
});

test('approvals empty state shows actionable all-caught-up message and hides redundant pagination', async () => {
  vi.mocked(api).mockResolvedValue({ items: [], truncated: false });

  render(<ApprovalsPage />);

  expect(await screen.findByRole('heading', { level: 1, name: 'Approvals' })).toBeInTheDocument();
  expect(await screen.findByText(/All caught up/i)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: /Browse all runs/i })).toHaveAttribute('href', '/runs');
  expect(screen.getByRole('link', { name: /Inspect audit log/i })).toHaveAttribute('href', '/audit');
  // Redundant pagination buttons hidden when empty
  expect(screen.queryByRole('navigation', { name: 'Approval pages' })).not.toBeInTheDocument();
});

test('populated approvals render card stack and pagination navigation', async () => {
  vi.mocked(api).mockResolvedValue({
    items: [
      {
        run_id: 'run-1',
        task_id: 'task-1',
        task_title: 'Implement OAuth callback',
        project_id: 'project-1',
        project_name: 'Forge',
        gate: 'plan',
        evidence_digest: 'e'.repeat(64),
        run_version: 1,
        policy_version: 1,
      },
    ],
    truncated: true,
  });

  render(<ApprovalsPage />);

  expect(await screen.findByText('Implement OAuth callback')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Review plan evidence' })).toHaveAttribute('href', '/runs/run-1');
  expect(screen.getByRole('navigation', { name: 'Approval pages' })).toBeInTheDocument();
});
