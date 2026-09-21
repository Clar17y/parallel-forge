import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { SubscriptionRuntimeStatus } from './runtime-status';

const observed = '2026-09-12T20:00:00Z';
const page = (workers: unknown[] = [], has_more = false) => ({ observed_at: observed, fresh_for_seconds: 45, workers, has_more });
const worker = (state: string, routes: unknown[] = []) => ({ worker_instance_id: state, state, last_seen_at: observed, stopped_at: state === 'stopped' ? observed : null, routes });
const route = (change: Record<string, unknown> = {}) => ({
  schema_version: 2,
  provider: 'openai',
  client: 'codex_app_server',
  model: 'gpt-6-astra',
  effort: 'medium',
  auth_mode: 'subscription',
  billing_mode: 'allowance_only',
  configured: true,
  admitted: false,
  reason: 'evidence_missing',
  effective_reason: 'evidence_missing',
  quota: 'unknown',
  evidence: [],
  quota_revision: null,
  quota_reset_at: null,
  quota_next_probe_at: null,
  ...change,
});
const response = (value: unknown) => new Response(JSON.stringify(value));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('missing reports remain unknown and refresh can reveal unregistered workers', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response(page()))
    .mockResolvedValueOnce(response(page([worker('current')])));
  vi.stubGlobal('fetch', fetcher);
  render(<SubscriptionRuntimeStatus />);
  expect(await screen.findByText(/Worker registration is unknown/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Refresh subscription readiness' }));
  expect(await screen.findByText(/No subscription routes registered by this worker/)).toBeInTheDocument();
  expect(screen.getByText(/It does not confirm sign-in, capability, or remaining allowance/)).toBeInTheDocument();
  expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/subscription-runtime?offset=0&limit=25');
  expect(screen.getByText(/never launches a provider client or reserves quota/)).toBeInTheDocument();
});

test('stale and stopped inventories retain their labels and exact route billing', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(page([
    worker('stale', [route({ effort: 'low', reason: 'ready', effective_reason: 'stale_worker', admitted: true, quota: 'eligible' })]),
    worker('stopped'),
  ]))));
  render(<SubscriptionRuntimeStatus />);
  expect(await screen.findByText(/Stale report — current registration is unknown/)).toBeInTheDocument();
  expect(screen.getByText(/Worker stopped reporting/)).toBeInTheDocument();
  expect(screen.getByText(/gpt-6-astra · low · subscription · allowance_only/)).toBeInTheDocument();
  expect(screen.queryByText(/No subscription routes registered by this worker/)).not.toBeInTheDocument();
});

test('ready routes show exact admission, durable evidence freshness, and bounded quota wording', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(page([
    worker('current', [route({
      admitted: true,
      reason: 'ready',
      effective_reason: 'ready',
      quota: 'eligible',
      quota_revision: 7,
      evidence: [{
        scope: 'astra-primary', evidence_id: '11111111-1111-4111-8111-111111111111', revision: 3,
        observed_at: '2026-09-12T19:45:00Z', expires_at: '2026-09-12T20:45:00Z',
      }],
    })]),
  ]))));
  render(<SubscriptionRuntimeStatus />);
  expect(await screen.findByText('Capability ready')).toBeInTheDocument();
  expect(screen.getByText(/Configured: yes · Admitted: yes/)).toBeInTheDocument();
  expect(screen.getByText(/eligible to attempt; this is not a remaining-allowance balance/i)).toBeInTheDocument();
  expect(screen.getByText(/astra-primary · evidence 11111111-1111-4111-8111-111111111111 · revision 3/)).toBeInTheDocument();
  expect(screen.getByText(/expires/)).toBeInTheDocument();
});

test('signed-out Codex routes give official subscription login guidance without paid credentials', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(page([
    worker('current', [route({ reason: 'signed_out', effective_reason: 'signed_out' })]),
  ]))));
  render(<SubscriptionRuntimeStatus />);
  expect(await screen.findByText('Signed out')).toBeInTheDocument();
  expect(screen.getByText(/codex login/)).toBeInTheDocument();
  expect(screen.getByText(/Forge never collects or copies provider credentials/i)).toBeInTheDocument();
  expect(screen.getByText(/unknown; this is not a zero-balance or availability claim/i)).toBeInTheDocument();
  expect(screen.queryByText(/buy|top.?up|enter an api key/i)).not.toBeInTheDocument();
});

test('missing evidence points to offline verification before live publication', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(page([
    worker('current', [route()]),
  ]))));
  render(<SubscriptionRuntimeStatus />);
  expect(await screen.findByText('Capability evidence missing')).toBeInTheDocument();
  expect(screen.getByText(/subscription-capabilities status and verify offline/i)).toBeInTheDocument();
  expect(screen.queryByText(/buy|top.?up|enter an api key/i)).not.toBeInTheDocument();
});

test('unproved isolation and confirmed quota exhaustion stay blocked with actionable safe copy', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(page([
    worker('current', [
      route({ provider: 'google', client: 'antigravity_cli', model: 'gemini-3.8-flash', reason: 'isolation_unproved', effective_reason: 'isolation_unproved' }),
      route({ admitted: true, reason: 'ready', effective_reason: 'quota_exhausted', quota: 'blocked', quota_revision: 9, quota_reset_at: '2026-09-13T01:00:00Z', quota_next_probe_at: '2026-09-13T01:05:00Z' }),
    ]),
  ]))));
  render(<SubscriptionRuntimeStatus />);
  expect(await screen.findByText('Isolation unproved')).toBeInTheDocument();
  expect(screen.getByText(/Signing in cannot resolve this isolation blocker/i)).toBeInTheDocument();
  expect(screen.getByText('Quota exhausted')).toBeInTheDocument();
  expect(screen.getByText(/Wait for the retained reset or next-probe time/i)).toBeInTheDocument();
  expect(screen.getByText('2026-09-13T01:00:00Z', { selector: 'time' })).toBeInTheDocument();
  expect(screen.getByText('2026-09-13T01:05:00Z', { selector: 'time' })).toBeInTheDocument();
});

test('a refresh signal from another tab reloads readiness metadata', async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(response(page([worker('current', [route({ reason: 'signed_out', effective_reason: 'signed_out' })])])))
    .mockResolvedValueOnce(response(page([worker('current', [route({ admitted: true, reason: 'ready', effective_reason: 'ready', quota: 'eligible' })])])));
  vi.stubGlobal('fetch', fetcher);
  render(<SubscriptionRuntimeStatus />);
  expect(await screen.findByText('Signed out')).toBeInTheDocument();
  window.dispatchEvent(new StorageEvent('storage', { key: 'forge:subscription-runtime:refresh', newValue: '1' }));
  expect(await screen.findByText('Capability ready')).toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(2);
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
