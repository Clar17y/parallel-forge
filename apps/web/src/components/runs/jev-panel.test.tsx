import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { JevPanel } from './jev-panel';
import { api } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });

const report = { schema_version: 1, run_id: 'run-1', requested_mode: 'shadow', effective_mode: 'not_yet_observed',
  requested_model: 'custom-alias', actual_model: null, calls: 0, attempts: 0, cache_hits: 0, unknown: 0,
  actual_input_units: 0, actual_output_units: 0, reserved_input_units: 20, duration_ms: 0,
  remaining_requests: 64, remaining_input_units: 249980, by_kind: {}, by_status: {}, review_focus_available: false, availability: 'no_samples' };

test('shows configured settings, not-yet-observed worker status, and per-run budgets', async () => {
  vi.mocked(api).mockResolvedValue(report);
  render(<JevPanel runId="run-1" />);
  expect(await screen.findByText('Configured mode: Shadow · observed mode: Not yet observed')).toBeInTheDocument();
  expect(screen.getByText('Shadow mode records the ranking projection while agents keep the original results.')).toBeInTheDocument();
  expect(screen.getByText('Requested model: custom-alias · reported model: Not yet observed')).toBeInTheDocument();
  expect(screen.getByText('Worker availability: No samples')).toBeInTheDocument();
  expect(screen.getByText(/64 requests and 249980 input allowance remaining/)).toBeInTheDocument();
  expect(screen.getByText(/0 reported input tokens · 0 reported output tokens/)).toBeInTheDocument();
  expect(screen.getByText('No Jev activity has been recorded for this run.')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Refresh Jev report' }));
  expect(vi.mocked(api).mock.calls.some(([path]) => path === '/runs/run-1/jev')).toBe(true);
});

test('reports errors and supports retry', async () => {
  let failed = true;
  vi.mocked(api).mockImplementation(async <T,>() => {
    if (failed) { failed = false; throw new Error('unavailable'); }
    return report as T;
  });
  render(<JevPanel runId="run-1" />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Jev report unavailable');
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(await screen.findByText('No Jev activity has been recorded for this run.')).toBeInTheDocument();
});

test('shows durable unavailable attempts even when no provider call was admitted', async () => {
  vi.mocked(api).mockResolvedValue({ ...report, attempts: 1, availability: 'degraded', by_kind: { semantic_search: 1 }, by_status: { unavailable: 1 } });
  render(<JevPanel runId="run-1" />);
  expect(await screen.findByText('Worker availability: Degraded')).toBeInTheDocument();
  expect(screen.getByText('Semantic search: 1')).toBeInTheDocument();
  expect(screen.getByText('Unavailable: 1')).toBeInTheDocument();
  expect(screen.getByText('1 attempt · 0 provider calls · 0 cache hits · 0 unknown outcomes')).toBeInTheDocument();
  expect(screen.queryByText('No Jev activity has been recorded for this run.')).not.toBeInTheDocument();
});

test('explains zero-cost refusals separately from reported tokens', async () => {
  vi.mocked(api).mockResolvedValue({ ...report, attempts: 1, availability: 'degraded',
    by_status: { budget_exhausted: 1 }, by_diagnostic: { budget_exhausted: 1 } });
  render(<JevPanel runId="run-1" />);
  expect(await screen.findByText('Worker availability: Degraded')).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Recorded limitations' })).toBeInTheDocument();
  expect(screen.getAllByText('Budget exhausted: 1')).toHaveLength(2);
  expect(screen.getByText(/Input allowance reserves one unit per request byte/)).toBeInTheDocument();
});
