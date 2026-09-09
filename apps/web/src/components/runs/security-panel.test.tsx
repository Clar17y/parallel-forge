import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { SecurityPanel } from './security-panel';
import { api } from '@/lib/api/client';
import { projection } from '@/test/projection';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });
test('security discloses frozen policy and invalidated approval evidence without granting authority', async () => {
  vi.mocked(api).mockResolvedValue({ items: [{ id: 'approval-1', gate: 'pr', evidence_digest: 'e'.repeat(64),
    run_version: 3, policy_version: 1, authenticated_actor_id: 'operator-1', created_at: '2026-09-08T10:00:00Z',
    invalidated_at: '2026-09-08T11:00:00Z', invalidation_reason: 'candidate changed' }], truncated: true });
  const value = projection();
  value.security.commands = [{ name: 'install', network_enabled: true }, { name: 'unit', network_enabled: false }];
  render(<SecurityPanel projection={value} />);
  expect(await screen.findByText(/Invalidated.*candidate changed/)).toBeInTheDocument();
  expect(screen.getByText('install: network allowed')).toBeInTheDocument();
  expect(screen.getByText('unit: network disabled')).toBeInTheDocument();
  expect(screen.getByText('.env')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Approved evidence' })).toHaveAttribute('href', `/api/artifacts/${'e'.repeat(64)}/download`);
  expect(screen.queryByRole('button', { name: /approve|merge/i })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Older approvals' }));
  expect(vi.mocked(api).mock.calls.at(-1)?.[0]).toBe('/runs/run-1/approval-history?offset=25&limit=25');
});

test('trusted host disclosure does not claim container or network isolation', async () => {
  vi.mocked(api).mockResolvedValue({ items: [], truncated: false });
  const value = projection();
  value.security.runner_mode = 'trusted_host';
  value.security.trusted_project = true;
  render(<SecurityPanel projection={value} />);
  expect(await screen.findByText('No approvals recorded on this page.')).toBeInTheDocument();
  expect(screen.getByText('Trusted host · unsandboxed')).toBeInTheDocument();
  expect(screen.getByText(/Network flags do not enforce isolation/)).toBeInTheDocument();
  expect(screen.queryByText(/Commands run as UID/)).not.toBeInTheDocument();
});
