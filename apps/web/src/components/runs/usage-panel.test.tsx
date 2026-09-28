import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { UsagePanel } from './usage-panel';
import { api } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });

function mockLegacyUsage(value: unknown) {
  vi.mocked(api).mockImplementation(async <T,>(path: string) => path.startsWith('/subscription-usage')
    ? { items: [], has_more: false } as T
    : path.endsWith('/jev') ? emptyJev as T : value as T);
}

const emptyJev = { schema_version: 1, run_id: 'run-1', requested_mode: 'off', effective_mode: 'not_yet_observed',
  requested_model: 'jev-latest', actual_model: null, calls: 0, attempts: 0, cache_hits: 0, unknown: 0,
  actual_input_units: 0, actual_output_units: 0, reserved_input_units: 0, duration_ms: 0,
  remaining_requests: 64, remaining_input_units: 250000, by_kind: {}, by_status: {}, review_focus_available: false, availability: 'off' };

test('run usage also exposes subscription measurements', async () => {
  vi.mocked(api).mockImplementation(async <T,>(path: string) => (path.endsWith('/jev') ? emptyJev : { items: [], truncated: false, has_more: false }) as T);
  render(<UsagePanel runId="run-1" />);
  expect(screen.getByRole('region', { name: 'Subscription usage' })).toBeInTheDocument();
  expect(await screen.findByText('No subscription attempts on this page.')).toBeInTheDocument();
  expect(vi.mocked(api).mock.calls.some(([path]) => path === '/subscription-usage?run_id=run-1&offset=0&limit=25&include_assessment=true')).toBe(true);
});
test('usage keeps actual tokens and unknown estimates distinct and pages historical calls', async () => {
  mockLegacyUsage({ items: [{ id: 'call-1', agent_execution_id: 'execution-1',
    role: 'planner', provider: 'fixture', model: 'test', prompt_version: 'v1', instruction_digest: null,
    input_tokens: 12, output_tokens: 3, cached_input_tokens: 2, duration_ms: 125, tool_call_count: 4,
    pricing_version: 'prices-v1', estimated_cost_minor: null, currency: 'USD', unknown_price_reason: 'No price',
    created_at: '2026-09-08T10:00:00Z' }], truncated: true });
  render(<UsagePanel runId="run-1" />);
  expect(await screen.findByText('12 input · 3 output · 2 cached input')).toBeInTheDocument();
  expect(screen.getByText('Unknown estimate · USD · No price')).toBeInTheDocument();
  expect(screen.getByText('Historical prompt digest unavailable')).toBeInTheDocument();
  expect(screen.getByText('Pricing version: prices-v1')).toBeInTheDocument();
  expect(screen.getByText('125 ms · 4 tool calls')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Older usage' }));
  expect(vi.mocked(api).mock.calls.at(-1)?.[0]).toBe('/runs/run-1/usage?offset=25&limit=25');
});

test('usage preserves a recorded zero estimate and historical digest', async () => {
  mockLegacyUsage({ items: [{ id: 'call-2', agent_execution_id: 'execution-2',
    role: 'reviewer', provider: 'fixture', model: 'test', prompt_version: 'old-version', instruction_digest: 'd'.repeat(64),
    input_tokens: 1, output_tokens: 1, cached_input_tokens: 0, duration_ms: 0, tool_call_count: 0,
    pricing_version: 'old-prices', estimated_cost_minor: 0, currency: 'GBP', unknown_price_reason: null,
    created_at: '2026-09-08T10:00:00Z' }], truncated: false });
  render(<UsagePanel runId="run-1" />);
  expect(await screen.findByText('0 minor units · GBP')).toBeInTheDocument();
  expect(screen.getByText('d'.repeat(64))).toBeInTheDocument();
  expect(screen.getByText('Prompt version: old-version')).toBeInTheDocument();
  expect(screen.queryByText(/Unknown estimate/)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Older usage' })).toBeDisabled();
});

test('usage failure is recoverable without displaying an invented empty result', async () => {
  let failed = false;
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path.startsWith('/subscription-usage')) return { items: [], has_more: false } as T;
    if (path.endsWith('/jev')) return emptyJev as T;
    if (!failed) { failed = true; throw new Error('offline'); }
    return { items: [], truncated: false } as T;
  });
  render(<UsagePanel runId="run-1" />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Usage unavailable');
  expect(screen.queryByText('No usage recorded on this page.')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(await screen.findByText('No usage recorded on this page.')).toBeInTheDocument();
});
