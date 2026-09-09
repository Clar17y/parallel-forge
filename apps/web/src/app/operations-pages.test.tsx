import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import UsagePage from './usage/page';
import AuditPage from './audit/page';
import EvaluationsPage from './evaluations/page';
import { api } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });
test('usage shows dimensions, distinguishes unpriced calls and requests the next server page', async () => {
  vi.mocked(api).mockResolvedValue({ items: [{ project_id: 'p1', run_id: 'r1', provider: 'fixture', model: 'model-a', currency: 'USD', input_tokens: 20, output_tokens: 4, duration_ms: 200, known_cost_minor: 7, unpriced_calls: 1, model_calls: 2 }], truncated: true });
  render(<UsagePage />);
  expect(await screen.findByText('fixture / model-a')).toBeInTheDocument();
  expect(screen.getByText('7 USD minor units')).toBeInTheDocument();
  expect(screen.getByText('1 call with unknown cost')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Run r1' })).toHaveAttribute('href', '/runs/r1');
  await userEvent.click(screen.getByRole('button', { name: 'Next' }));
  expect(vi.mocked(api).mock.calls.at(-1)?.[0]).toBe('/usage?offset=25&limit=25');
});
test.each([false, true])('evaluations preserve persisted status and prompt lineage (usage=%s)', async known => {
  vi.mocked(api).mockResolvedValue({ items: [{ suite_id: 's1', suite_status: 'running', fixture_version: 'f1', metric_version: 'm1', case_id: 'c1', case_key: 'example', role: 'reviewer', status: 'failed', metrics: { score: 0.5 }, prompt_version: known ? 'recorded-v2' : null, provider: known ? 'fixture' : null, model: known ? 'test-model' : null, input_tokens: null, output_tokens: null, duration_ms: null, currency: null, estimated_cost_minor: null, input_artifact_digest: 'a'.repeat(64), output_artifact_digest: null }], truncated: false });
  render(<EvaluationsPage />);
  expect(await screen.findByText('example')).toBeInTheDocument();
  expect(screen.getByText('failed')).toBeInTheDocument();
  if (known) {
    expect(screen.getByText('Prompt version: recorded-v2')).toBeInTheDocument();
    expect(screen.getByText('Estimated cost: Unknown')).toBeInTheDocument();
  } else {
    expect(screen.getByText('Model usage unavailable')).toBeInTheDocument();
    expect(screen.getByText('Prompt version: Unavailable')).toBeInTheDocument();
  }
  expect(screen.getByRole('link', { name: 'Input evidence' })).toHaveAttribute('href', `/api/artifacts/${'a'.repeat(64)}/download`);
  expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
});

test('failed usage load exposes retry and clears the error after successful refresh', async () => {
  vi.mocked(api).mockRejectedValueOnce(new Error('unavailable')).mockResolvedValueOnce({ items: [], truncated: false });
  render(<UsagePage />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Usage unavailable');
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(await screen.findByText('No usage on this page.')).toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});


test('audit applies source-aware filters and links exact event evidence', async () => {
  vi.mocked(api).mockResolvedValue({ items: [{ id: 'event-1', source: 'run', actor_id: null, actor_class: 'worker',
    run_id: 'run-1', project_id: 'project-1', event_type: 'tool.observed', created_at: '2026-09-08T10:00:00Z',
    operations: [{ id: 'op-1', kind: 'push_branch', status: 'PENDING' }], payload: { note: '<script>untrusted</script>' } }], truncated: true });
  render(<AuditPage />);
  expect(await screen.findByText('tool.observed')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Event evidence' })).toHaveAttribute('href', '/api/audit/run-events/event-1');
  await userEvent.type(screen.getByLabelText('Run ID'), '11111111-1111-4111-8111-111111111111');
  await userEvent.selectOptions(screen.getByLabelText('Current operation status'), 'PENDING');
  await userEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
  expect(vi.mocked(api).mock.calls.at(-1)?.[0]).toContain('run_id=11111111-1111-4111-8111-111111111111');
  expect(vi.mocked(api).mock.calls.at(-1)?.[0]).toContain('operation_status=PENDING');
  expect(document.querySelector('script')).toBeNull();
});
