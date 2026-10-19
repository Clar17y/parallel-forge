import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { ActivityPanel } from './activity-panel';
import { api } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });
test('activity requests this run, shows denied actions and actor class, and pages through evidence', async () => {
  vi.mocked(api).mockResolvedValue({ items: [{ id: 'event-1', source: 'run', run_id: 'run-1', project_id: 'project-1',
    event_type: 'tool.denied', actor_class: 'agent', actor_id: null, created_at: '2026-09-08T10:00:00Z',
    operations: [], payload: { reason: 'not authorized' } }], truncated: true });
  render(<ActivityPanel runId="run-1" />);
  expect(await screen.findByText('tool.denied')).toBeInTheDocument();
  expect(screen.getByText(/agent · No actor ID/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Event evidence' })).toHaveAttribute('href', '/api/audit/run-events/event-1');
  expect(vi.mocked(api).mock.calls[0][0]).toBe('/audit?run_id=run-1&offset=0&limit=25');
  await userEvent.click(screen.getByRole('button', { name: 'Older activity' }));
  expect(vi.mocked(api).mock.calls.at(-1)?.[0]).toBe('/audit?run_id=run-1&offset=25&limit=25');
});

test('failure filtering uses recorded outcomes for failed checks, timeouts and denied calls', async () => {
  const payloads = [
    { tool_name: 'repository.read_file', path: 'failed.txt', status: 'failed' },
    { tool_name: 'repository.read_file', path: 'timeout.txt', status: 'succeeded', timed_out: true },
    { tool_name: 'repository.read_file', path: 'denied.txt', status: 'succeeded', authorized: false },
    { tool_name: 'build.run_named_check', command_name: 'lint', status: 'succeeded', exit_code: 1 },
    { tool_name: 'repository.read_file', path: 'success.txt', status: 'succeeded' },
    { tool_name: 'repository.read_file', path: 'cancelled.txt', status: 'cancelled' },
    {},
  ];
  vi.mocked(api).mockResolvedValue({ items: payloads.map((payload, index) => ({
    id: `event-${index}`, source: 'run', run_id: 'run-1', project_id: 'project-1',
    event_type: index === 6 ? 'custom.error' : 'tool_call.completed', actor_class: 'agent', actor_id: null,
    created_at: '2026-09-08T10:00:00Z', operations: [], payload,
  })), truncated: false });
  render(<ActivityPanel runId="run-1" />);
  await screen.findByRole('heading', { name: 'Read success.txt' });
  await userEvent.selectOptions(screen.getByLabelText('Filter:'), 'failures');
  const alerts = screen.getAllByRole('article');
  expect(alerts).toHaveLength(5);
  expect(alerts.map(item => item.dataset.activityStatus)).toEqual(['failed', 'failed', 'denied', 'failed', 'unknown']);
  for (const article of alerts) expect(article).toHaveStyle({ borderColor: 'var(--danger)' });
  expect(screen.queryByRole('heading', { name: 'Read success.txt' })).not.toBeInTheDocument();
  await userEvent.selectOptions(screen.getByLabelText('Filter:'), 'routine');
  expect(screen.getAllByRole('article')).toHaveLength(2);
  expect(screen.getByRole('heading', { name: 'Read success.txt' })).toBeVisible();
  expect(screen.getByRole('heading', { name: /cancelled.txt/ })).toBeVisible();
});
