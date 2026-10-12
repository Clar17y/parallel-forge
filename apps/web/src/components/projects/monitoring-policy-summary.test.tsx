import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';
import type { MonitoringPolicyResponse, TaskUsageHistoryResponse } from '@/lib/api/monitoring-policy';
import { MonitoringPolicySummary } from './monitoring-policy-summary';

afterEach(cleanup);

const projectId = '11111111-1111-4111-8111-111111111111';
const policy: MonitoringPolicyResponse = {
  project_id: projectId, revision: 2, mode: 'checkpoint', warning_multiplier: 1.5,
  checkpoint_multiplier: 2, history_limit: 50, minimum_comparables: 20,
  window_days: null, hard_token_cap: null, hard_context_cap: null,
  manual_estimate_tokens: null, allow_cross_project_fallback: true,
};
const report: TaskUsageHistoryResponse = {
  as_of: '2026-10-11T12:00:00Z',
  overview: { status: 'insufficient_history', sample_count: 5, eligible_count: 7,
    coverage_count: 5, excluded_count: 2, measured_coverage: 5 / 7,
    mean_tokens: 0, median_tokens: 0, p90_tokens: 0, reference_tokens: null,
    cohort_source: 'project_exact', labels: [], project_ids: [projectId], phase_role_breakdown: [] },
  filters: { project_ids: [projectId], history_limit: 100, minimum_comparables: 30,
    window_days: 14, subject_kind: null, task_kind: null, provider: null, model: null, effort: null },
  comparable_references: {}, by_task_type: {}, by_model: {}, by_provider: {},
  by_effort: {}, by_project: {}, excluded_outcomes: {}, failed_attempt_count: 0,
};

test('uses the report minimum when it exceeds the saved policy minimum and labels actual filters', () => {
  render(<MonitoringPolicySummary policy={policy} report={report} />);
  expect(screen.getByText(/Minimum comparables: 20 samples/i)).toBeInTheDocument();
  expect(screen.getByText(/Insufficient history: 5 \/ 30 reported samples/i)).toBeInTheDocument();
  expect(screen.getByText(/Report sampling: 100 completions/i)).toHaveTextContent(/14 days/i);
  expect(screen.getByText(/Report sampling: 100 completions/i)).toHaveTextContent(projectId);
  expect(screen.getByText(/Measured average: 0 tokens \(measured zero\)/i)).toBeInTheDocument();
});

test('uses the report minimum when it is below the saved policy minimum, including ready status', () => {
  render(<MonitoringPolicySummary policy={{ ...policy, minimum_comparables: 40 }} report={{
    ...report, filters: { ...report.filters, minimum_comparables: 10 },
    overview: { ...report.overview, status: 'ready', sample_count: 12, mean_tokens: 120 },
  }} />);
  expect(screen.getByText(/Minimum comparables: 40 samples/i)).toBeInTheDocument();
  expect(screen.getByText(/Report status: ready for these sampling rules \(minimum 10 samples\)/i)).toBeInTheDocument();
  expect(screen.queryByText(/40 reported samples/i)).not.toBeInTheDocument();
});

test('reports unknown and missing measured history without inventing a policy fallback', () => {
  const { rerender } = render(<MonitoringPolicySummary policy={policy} report={{ ...report,
    overview: { ...report.overview, mean_tokens: null, median_tokens: null, p90_tokens: null },
  }} />);
  expect(screen.getByText(/Measured average: Unknown/i)).toBeInTheDocument();
  rerender(<MonitoringPolicySummary policy={policy} report={{ ...report,
    overview: { ...report.overview, status: 'ready', mean_tokens: null, median_tokens: null, p90_tokens: null },
  }} />);
  expect(screen.getAllByText('Unknown', { selector: 'span' })).toHaveLength(3);
  rerender(<MonitoringPolicySummary policy={policy} report={null} />);
  expect(screen.getByText(/Usage report unavailable; readiness unknown/i)).toBeInTheDocument();
  expect(screen.queryByText(/Insufficient history:/i)).not.toBeInTheDocument();
});

test('formats exact signed64 caps and manual estimates with comma grouping', () => {
  render(<MonitoringPolicySummary policy={{
    ...policy,
    hard_token_cap: '9007199254740992',
    hard_context_cap: '9007199254740993',
    manual_estimate_tokens: '9223372036854775807',
  }} report={null} />);
  expect(screen.getByText(/Hard token cap: 9,007,199,254,740,992 tokens/i)).toBeInTheDocument();
  expect(screen.getByText(/Hard context cap: 9,007,199,254,740,993 tokens/i)).toBeInTheDocument();
  expect(screen.getByText(/Manual estimate: 9,223,372,036,854,775,807 tokens/i)).toBeInTheDocument();
});
