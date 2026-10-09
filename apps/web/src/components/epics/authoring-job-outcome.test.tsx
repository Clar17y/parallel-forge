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
