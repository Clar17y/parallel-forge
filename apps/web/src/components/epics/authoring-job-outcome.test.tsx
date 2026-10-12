import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';
import { AuthoringJobOutcome } from './authoring-job-outcome';
import type { AuthoringOutcome } from '@/hooks/epics/types';

afterEach(cleanup);

test('labels each observed job with its frozen model and handles older outcomes', () => {
  const base = { schema_version: 1, job_id: 'job-A', job_version: 1, state: 'running',
    process_settled: false, usage_known: null } as AuthoringOutcome;
  const { rerender } = render(<AuthoringJobOutcome outcome={{ ...base, route: {
    provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'low',
  } }} />);
  expect(screen.getByText('Model: gpt-6-astra · Effort: low')).toBeInTheDocument();
  rerender(<AuthoringJobOutcome outcome={{ ...base, job_id: 'job-B', route: {
    provider: 'openai', client: 'codex_app_server', model: 'gpt-6-luna', effort: 'medium',
  } }} />);
  expect(screen.getByText('Model: gpt-6-luna · Effort: medium')).toBeInTheDocument();
  rerender(<AuthoringJobOutcome outcome={base} />);
  expect(screen.queryByText(/Model:/)).not.toBeInTheDocument();
});

test('renders concise sanitized diagnostic timeline and attempt identity in EvidenceDetails', () => {
  const outcome: AuthoringOutcome = {
    schema_version: 1,
    job_id: 'job-diag',
    job_version: 2,
    state: 'failed',
    failure: 'timeout',
    process_settled: true,
    usage_known: true,
    diagnostics: {
      attempt_id: 'attempt-f74c31a9',
      dropped_events: 3,
      events: [
        {
          sequence: 0,
          kind: 'launch',
          occurred_at: '2026-10-09T20:22:06Z',
          elapsed_ms: 0,
        },
        {
          sequence: 1,
          kind: 'tool_started',
          operation: 'repository_read',
          tool_name: 'list_files',
          occurred_at: '2026-10-09T20:22:07Z',
          elapsed_ms: 1200,
        },
        {
          sequence: 2,
          kind: 'tool_completed',
          operation: 'repository_read',
          tool_name: 'list_files',
          duration_ms: 350,
          occurred_at: '2026-10-09T20:22:07.350Z',
          elapsed_ms: 1550,
        },
        {
          sequence: 3,
          kind: 'provider_wait',
          operation: 'provider_receive',
          occurred_at: '2026-10-09T20:22:08Z',
          elapsed_ms: 2000,
        },
        {
          sequence: 4,
          kind: 'process_settled',
          operation: 'process_settlement',
          occurred_at: '2026-10-09T20:27:06Z',
          elapsed_ms: 300059,
        },
      ],
    },
  };

  render(<AuthoringJobOutcome outcome={outcome} />);

  expect(screen.getByText('Inspect diagnostic timeline')).toBeInTheDocument();
  expect(screen.getByText('attempt_id: attempt-f74c31a9')).toBeInTheDocument();
  expect(screen.getByText('dropped_events: 3')).toBeInTheDocument();
  expect(screen.getByText(/#0 · 2026-10-09 20:22:06 UTC · Process launched · elapsed 0 ms/)).toBeInTheDocument();
  expect(screen.getByText(/#1 · 2026-10-09 20:22:07 UTC · Repository read started \(List files\) · elapsed 1,200 ms/)).toBeInTheDocument();
  expect(screen.getByText(/#2 · 2026-10-09 20:22:07 UTC · Repository read completed \(List files\) · elapsed 1,550 ms · duration 350 ms/)).toBeInTheDocument();
  expect(screen.getByText(/#3 · 2026-10-09 20:22:08 UTC · Waiting for model · elapsed 2,000 ms/)).toBeInTheDocument();
  expect(screen.getByText(/#4 · 2026-10-09 20:27:06 UTC · Process settled · elapsed 300,059 ms/)).toBeInTheDocument();
});

test('older outcomes without diagnostics do not render diagnostic timeline', () => {
  const outcome: AuthoringOutcome = {
    schema_version: 1,
    job_id: 'job-old',
    job_version: 1,
    state: 'running',
    process_settled: false,
    usage_known: null,
    diagnostics: null,
  };

  render(<AuthoringJobOutcome outcome={outcome} />);
  expect(screen.queryByText('Inspect diagnostic timeline')).not.toBeInTheDocument();
});
