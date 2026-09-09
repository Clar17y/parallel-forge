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
