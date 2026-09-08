import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { api } from '@/lib/api/client';
import { ChecksPanel } from './checks-panel';
import { ReviewPanel } from './review-panel';
import { projection } from '@/test/projection';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));
beforeEach(() => vi.mocked(api).mockResolvedValue({ items: [], truncated: false }));
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });
test('remote checks retain their observed head and link the immutable evidence', async () => {
  const value = projection({ remote_observation: { observation_digest: 'a'.repeat(64), head_sha: 'b'.repeat(40),
    checks: [{ name: 'ci', status: 'completed', conclusion: 'failure', head_sha: 'b'.repeat(40), summary: '<script>untrusted</script>', text: 'Failure details' }], reviews: [] } });
  render(<ChecksPanel projection={value} />);
  expect(await screen.findByText('No local validation checks recorded.')).toBeInTheDocument();
  expect(screen.getByText('failure')).toBeInTheDocument();
  expect(screen.getAllByText('b'.repeat(40)).length).toBeGreaterThan(0);
  expect(screen.getByText('<script>untrusted</script>')).toBeInTheDocument();
  expect(document.querySelector('script')).toBeNull();
  expect(screen.getByRole('link', { name: 'Remote observation evidence' })).toHaveAttribute('href', `/api/artifacts/${'a'.repeat(64)}/download`);
});

test('review distinguishes local severity and resolution from remote blocking threads', () => {
  const value = projection({ remote_observation: { observation_digest: 'a'.repeat(64), head_sha: 'b'.repeat(40), checks: [],
    reviews: [{ reviewer: 'reviewer', state: 'CHANGES_REQUESTED', submitted_at: null, requested_changes: true, unresolved_threads: 2, comment_count: 3, body: 'Please fix', feedback: ['Missing case'] }] } });
  value.review.findings = [{ id: 'R1', severity: 'major', path: 'src/app.py', start_line: 12, summary: 'Race', evidence: 'Observed twice', proposed_resolution: 'Serialize', status: 'RESOLVED' }];
  render(<ReviewPanel projection={value} />);
  expect(screen.getByRole('heading', { name: 'Major' })).toBeInTheDocument();
  expect(screen.getByText('RESOLVED')).toBeInTheDocument();
  expect(screen.getByText('2 unresolved threads')).toBeInTheDocument();
  expect(screen.getByText('Changes requested')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /merge/i })).not.toBeInTheDocument();
});

test('review shows an explicit request for changes even with no findings', async () => {
  const value = projection();
  value.review = { execution_id: 'reviewer-1', evidence_digest: 'a'.repeat(64), head_sha: 'b'.repeat(40), findings: [] };
  vi.mocked(api).mockResolvedValue({ digest: 'a'.repeat(64), run_id: value.run.id,
    producer_execution_id: 'reviewer-1', head_sha: 'b'.repeat(40), policy_version: value.run.policy_version,
    decision: 'request_changes', summary: 'More evidence required', tested_claims: [], missing_evidence: ['Tests unavailable'] });
  render(<ReviewPanel projection={value} />);
  expect(await screen.findByText('Decision: request changes')).toBeInTheDocument();
  expect(screen.getByText('Tests unavailable')).toBeInTheDocument();
});

test.each(['digest', 'run_id', 'producer_execution_id', 'head_sha', 'policy_version'])('review hides a decision with mismatched %s', async field => {
  const value = projection();
  value.review = { execution_id: 'reviewer-1', evidence_digest: 'a'.repeat(64), head_sha: 'b'.repeat(40), findings: [] };
  vi.mocked(api).mockResolvedValue({ digest: 'a'.repeat(64), run_id: value.run.id,
    producer_execution_id: 'reviewer-1', head_sha: 'b'.repeat(40), policy_version: value.run.policy_version,
    decision: 'approve', summary: 'Approved', tested_claims: [], missing_evidence: [], [field]: 'wrong' });
  render(<ReviewPanel projection={value} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Review decision evidence unavailable');
  expect(screen.queryByText('Decision: approve')).not.toBeInTheDocument();
});


test('local history shows attempt timing and follows server pagination', async () => {
  vi.mocked(api).mockResolvedValue({ items: [{ id: 'check-1', name: 'ci', command_name: 'ci', command_version: 1,
    status: 'FAILED', attempt: 1, duration_ms: 1250, configured_runner_mode: 'docker', exit_code: 1,
    head_sha: 'b'.repeat(40), output_artifact_digest: null, evidence_digest: 'a'.repeat(64) }], truncated: true });
  render(<ChecksPanel projection={projection()} />);
  expect(await screen.findByText('Attempt 1 · Duration: 1250 ms')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Validation evidence for attempt 1' })).toHaveAttribute('href', `/api/artifacts/${'a'.repeat(64)}/download`);
  await userEvent.click(screen.getByRole('button', { name: 'Next checks' }));
  expect(vi.mocked(api).mock.calls.at(-1)?.[0]).toBe('/runs/run-1/checks?offset=25&limit=25');
});
