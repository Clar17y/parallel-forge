import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { SubscriptionRuntimeStatus } from './runtime-status';

const observed = '2026-09-12T20:00:00Z';
const page = (workers: unknown[] = [], has_more = false) => ({ observed_at: observed, fresh_for_seconds: 45, workers, has_more });
const worker = (state: string, routes: unknown[] = []) => ({ worker_instance_id: state, state, last_seen_at: observed, stopped_at: state === 'stopped' ? observed : null, routes });
const response = (value: unknown) => new Response(JSON.stringify(value));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('missing reports remain unknown and refresh can reveal unregistered workers', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response(page()))
    .mockResolvedValueOnce(response(page([worker('current')])));
  vi.stubGlobal('fetch', fetcher);
  render(<SubscriptionRuntimeStatus />);
  expect(await screen.findByText(/Worker registration is unknown/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Refresh worker registration' }));
  expect(await screen.findByText(/No subscription routes registered by this worker/)).toBeInTheDocument();
  expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/subscription-runtime?offset=0&limit=25');
  expect(screen.getByText(/does not confirm sign-in, model access/)).toBeInTheDocument();
});

test('stale and stopped inventories retain their labels and exact route billing', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(page([
    worker('stale', [{ provider: 'openai', client: 'codex', model: 'gpt-6-astra', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' }]),
    worker('stopped'),
  ]))));
  render(<SubscriptionRuntimeStatus />);
  expect(await screen.findByText(/Stale report — current registration is unknown/)).toBeInTheDocument();
  expect(screen.getByText(/Worker stopped reporting/)).toBeInTheDocument();
  expect(screen.getByText(/gpt-6-astra · low · subscription · allowance_only/)).toBeInTheDocument();
  expect(screen.queryByText(/No subscription routes registered by this worker/)).not.toBeInTheDocument();
});

test('pagination is explicit and a failed refresh does not show old current state', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response(page([worker('current')], true)))
    .mockRejectedValueOnce(new Error('offline'));
  vi.stubGlobal('fetch', fetcher);
  render(<SubscriptionRuntimeStatus />);
  expect(await screen.findByText(/This page is not the complete worker inventory/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Next worker reports' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Worker registration unavailable');
  expect(screen.queryByText(/Current report/)).not.toBeInTheDocument();
  expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/subscription-runtime?offset=25&limit=25');
});
