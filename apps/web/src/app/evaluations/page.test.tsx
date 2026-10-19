import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import EvaluationsPage from './page';
import { api } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.mocked(api).mockReset();
});

test('evaluations empty state provides helpful guidance and hides redundant pagination', async () => {
  vi.mocked(api).mockResolvedValue({ items: [], truncated: false });

  render(<EvaluationsPage />);

  expect(await screen.findByRole('heading', { level: 1, name: 'Evaluations' })).toBeInTheDocument();
  expect(await screen.findByText(/No evaluations recorded/i)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: /Browse runs/i })).toHaveAttribute('href', '/runs');
  // Redundant pagination buttons hidden when empty
  expect(screen.queryByRole('navigation', { name: 'Evaluation pages' })).not.toBeInTheDocument();
});

test('populated evaluations render compact summary cards with status badges and secondary disclosure', async () => {
  vi.mocked(api).mockResolvedValue({
    items: [
      {
        suite_id: '04551e32-c8eb-4012-a758-f90156ab3dbb',
        suite_status: 'pending',
        fixture_version: 'readability-fixture-v1',
        metric_version: 'layout-checks-v1',
        case_id: 'c1',
        case_key: 'synthetic-layout-failed-long-requirements-and-exact-evidence-identifiers',
        role: 'developer',
        status: 'failed',
        metrics: { passed: false, duration_ms: 120 },
        prompt_version: null,
        provider: null,
        model: null,
        input_tokens: null,
        output_tokens: null,
        duration_ms: null,
        currency: null,
        estimated_cost_minor: null,
        input_artifact_digest: null,
        output_artifact_digest: null,
      },
    ],
    truncated: false,
  });

  render(<EvaluationsPage />);

  expect(await screen.findByText('synthetic-layout-failed-long-requirements-and-exact-evidence-identifiers')).toBeInTheDocument();
  // Status badge
  expect(screen.getByText('failed')).toBeInTheDocument();
  // Suite status
  expect(screen.getByText(/Suite: pending/i)).toBeInTheDocument();
  // Role
  expect(screen.getAllByText(/developer/).length).toBeGreaterThan(0);
  // Suite UUID in secondary details
  expect(screen.getByText(/04551e32-c8eb-4012-a758-f90156ab3dbb/)).toBeInTheDocument();
  // Metrics in bounded pre
  expect(screen.getByText('Recorded metrics')).toBeInTheDocument();
  // Pagination shown when populated
  expect(screen.getByRole('navigation', { name: 'Evaluation pages' })).toBeInTheDocument();
});
