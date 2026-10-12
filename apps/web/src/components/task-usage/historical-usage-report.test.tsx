import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HistoricalUsageReport } from './historical-usage-report';
import type { components } from '@/lib/api/schema';

const summary: components['schemas']['UsageHistorySummaryResponse'] = {
  status: 'ready', sample_count: 24, eligible_count: 30, coverage_count: 24, measured_coverage: 0.8,
  mean_tokens: 120000, median_tokens: 100000, p90_tokens: 210000, reference_tokens: 100000,
  warning_threshold_tokens: null, checkpoint_threshold_tokens: 210000, cohort_source: 'pooled_overview',
  labels: ['mixed_routes', 'implementation'], project_ids: ['project-a', 'project-b'],
  window_started_at: '2026-09-11T00:00:00Z', window_ended_at: '2026-10-11T12:00:00Z',
  phase_role_breakdown: [{ phase: 'implementation', role: 'primary', tokens: 2500000 },
    { phase: 'implementation', role: 'delegate', tokens: 380000 }], excluded_count: 6,
};
const report: components['schemas']['TaskUsageHistoryResponse'] = {
  as_of: '2026-10-11T12:00:00Z', overview: summary,
  filters: { project_ids: [], history_limit: 50, minimum_comparables: 20, window_days: 30 },
  comparable_references: { 'implementation:gemini:flash:medium': { ...summary, status: 'insufficient_history', sample_count: 4,
    cohort_source: 'exact_route', labels: ['exact_route'], mean_tokens: null, median_tokens: null, p90_tokens: null,
    measured_coverage: null, checkpoint_threshold_tokens: null, phase_role_breakdown: [] } },
  by_task_type: { implementation: summary }, by_model: { 'flash-3.8': summary }, by_provider: { gemini: summary },
  by_effort: { medium: summary }, by_project: { 'project-a': summary }, excluded_outcomes: {}, failed_attempt_count: 2,
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

test('shows pooled observed history, route mixture, coverage, and a separate insufficient comparable reference', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify(report), { status: 200 }));
  render(<HistoricalUsageReport />);

  expect(await screen.findByRole('region', { name: 'Historical task usage' })).toBeVisible();
  expect(screen.getByText(/Mean 120,000 tokens.*Median 100,000.*P90 210,000/)).toBeVisible();
  expect(screen.getByText(/24 measured samples.*80%.*observed 2026-09-11 to 2026-10-11/)).toBeVisible();
  expect(screen.getByText(/pooled overview.*mixed routes/i)).toBeVisible();
  expect(screen.getByText(/primary.*2,500,000 tokens/i)).toBeVisible();
  expect(screen.getByRole('heading', { name: 'Comparable checkpoint reference' })).toBeVisible();
  expect(screen.getByText(/Insufficient history.*4 samples/i)).toBeVisible();
  expect(screen.getByLabelText('Project filter')).toBeVisible();
  expect(screen.getByLabelText('Model filter')).toBeVisible();
});

test('keeps unknown and zero values distinct and reports unavailable history honestly', async () => {
  const unknownReport = { ...report, overview: { ...summary, status: 'insufficient_history' as const,
    mean_tokens: null, median_tokens: null, p90_tokens: null, measured_coverage: null, sample_count: 0,
    cohort_source: 'none', phase_role_breakdown: [] } };
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(unknownReport), { status: 200 }));
  render(<HistoricalUsageReport />);
  expect(await screen.findByText(/Mean Unknown.*Median Unknown.*P90 Unknown/)).toBeVisible();
  expect(screen.getByText(/0 measured samples.*coverage unknown/)).toBeVisible();
});

test('renders a measured zero instead of calling it unknown', async () => {
  const zeroReport = { ...report, overview: { ...summary, mean_tokens: 0 } };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify(zeroReport), { status: 200 }));
  render(<HistoricalUsageReport />);
  expect(await screen.findByText(/Mean 0 tokens/)).toBeVisible();
});

test('renders report-provided labels as text', async () => {
  const hostileLabel = '<img src=x onerror=alert(1)>';
  const labelledReport = { ...report, overview: { ...summary, labels: [hostileLabel] } };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify(labelledReport), { status: 200 }));
  render(<HistoricalUsageReport />);
  expect(await screen.findByText(/Report labels:/)).toHaveTextContent(hostileLabel);
  expect(document.querySelector('img')).toBeNull();
});

test('applies project, category and route filters through the report query', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify(report), { status: 200 }));
  const user = userEvent.setup();
  render(<HistoricalUsageReport />);
  await screen.findByRole('heading', { name: 'Pooled overview · mixed routes' });
  await user.type(screen.getByLabelText('Project filter'), 'project-a');
  await user.selectOptions(screen.getByLabelText('Task category filter'), 'implementation');
  await user.type(screen.getByLabelText('Provider filter'), 'gemini');
  await user.type(screen.getByLabelText('Model filter'), 'flash-3.8');
  await user.type(screen.getByLabelText('Effort filter'), 'medium');
  await user.click(screen.getByRole('button', { name: 'Apply filters' }));
  await screen.findAllByRole('heading', { name: 'Pooled overview · mixed routes' });
  const requested = String(fetch.mock.calls.at(-1)?.[0]);
  expect(requested).toContain('project_id=project-a');
  expect(requested).toContain('task_kind=implementation');
  expect(requested).toContain('subject_kind=run');
  expect(requested).toContain('provider=gemini');
  expect(requested).toContain('model=flash-3.8');
  expect(requested).toContain('effort=medium');
});
