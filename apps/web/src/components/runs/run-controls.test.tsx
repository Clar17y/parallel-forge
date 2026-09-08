import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, expect, test, vi } from 'vitest';
import { RunControls } from './run-controls';
import { projection } from '@/test/projection';
import { api, ApiError, mutate } from '@/lib/api/client';

vi.mock('@/lib/api/client', async original => ({ ...await original<typeof import('@/lib/api/client')>(), api: vi.fn(), mutate: vi.fn() }));
beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
});
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); vi.mocked(mutate).mockReset(); });
const evidence = { base_sha: 'a'.repeat(40), plan_digest: 'c'.repeat(64), policy_version: 2, token_budget: 116000 };

test.each(['success', 'protection mismatch', 'base mismatch', 'unsafe protection', '409'])('merge confirmation handles %s with exact remote protection', async scenario => {
  const value = projection({ available_commands: [{ name: 'approve_merge', expected_run_version: 7, requires_feedback: false,
    gate: 'merge', evidence_digest: 'd'.repeat(64), policy_version: 2 }] });
  const merge = { repository: 'owner/repo', pull_request_number: 12, head_sha: 'a'.repeat(40), base_ref: 'main', base_sha: 'b'.repeat(40),
    required_checks: { ci: 'success' }, unresolved_blocking_findings: 0, validation_digest: 'c'.repeat(64), review_digest: 'e'.repeat(64),
    runner_mode: 'docker', runner_evidence_digest: '1'.repeat(64), protection_digest: '2'.repeat(64), merge_method: 'squash', policy_version: 2 };
  value.candidate.commit = merge.head_sha;
  value.candidate.validation_evidence_digest = merge.validation_digest; value.candidate.review_evidence_digest = merge.review_digest;
  value.pull_request = { repository: 'owner/repo', number: 12, head_sha: merge.head_sha, base_sha: merge.base_sha, base_ref: 'main', branch: 'feature', state: 'open', merge_state: null };
  value.remote_observation = { observation_digest: '3'.repeat(64), head_sha: merge.head_sha, checks: [], reviews: [] };
  vi.mocked(api).mockResolvedValueOnce({ digest: 'd'.repeat(64), text: JSON.stringify(merge) })
    .mockResolvedValueOnce({ digest: '3'.repeat(64), repository: 'owner/repo', pull_request_number: 12, head_sha: merge.head_sha, base_ref: 'main', observed_base_sha: scenario === 'base mismatch' ? '9'.repeat(40) : merge.base_sha, protection_digest: scenario === 'protection mismatch' ? '9'.repeat(64) : '2'.repeat(64), protection: {
      strict_required_checks: true, merge_queue_enabled: false, actor_can_bypass: scenario === 'unsafe protection', verified: true, required_check_names: ['ci'], evidence_source: 'branch-protection+rulesets' } })
    .mockResolvedValueOnce({ token: 'merge-challenge', expires_at: '2099-01-01T00:00:00Z' });
  if (scenario === '409') vi.mocked(api).mockRejectedValueOnce(new ApiError(409, 'stale'));
  else vi.mocked(api).mockResolvedValueOnce({ approval_id: 'approval-1' });
  const refresh = vi.fn().mockResolvedValue(value);
  render(<RunControls projection={value} onRefresh={refresh} />);
  await userEvent.click(screen.getByRole('button', { name: 'Approve merge' }));
  if (['protection mismatch', 'base mismatch', 'unsafe protection'].includes(scenario)) {
    expect(await screen.findByRole('alert')).toHaveTextContent('changed');
    expect(api).toHaveBeenCalledTimes(2); return;
  }
  const dialog = await screen.findByRole('dialog');
  expect(dialog).toHaveTextContent('owner/repo #12');
  expect(dialog).toHaveTextContent('branch-protection+rulesets');
  expect(dialog).toHaveTextContent('ci: success');
  expect(dialog).toHaveTextContent('squash');
  expect(dialog).toHaveTextContent(merge.base_sha);
  await userEvent.click(screen.getByRole('button', { name: 'Confirm approve merge' }));
  await waitFor(() => expect(api).toHaveBeenCalledTimes(4));
  expect(JSON.parse(vi.mocked(api).mock.calls[3][1]!.body as string)).toEqual({
    gate: 'merge', run_version: 7, evidence_digest: 'd'.repeat(64), challenge_token: 'merge-challenge',
  });
  if (scenario === '409') { expect(await screen.findByRole('alert')).toHaveTextContent('changed'); expect(refresh).toHaveBeenCalledTimes(2); }
  expect(mutate).not.toHaveBeenCalled();
});

test.each(['success', 'body mismatch', 'candidate mismatch', '409'])('PR approval handles %s with exact publication evidence', async scenario => {
  const value = projection({ available_commands: [{ name: 'approve_pr', expected_run_version: 7, requires_feedback: false,
    gate: 'pr', evidence_digest: 'd'.repeat(64), policy_version: 2 }] });
  const pr = { candidate_commit: 'a'.repeat(40), diff_digest: 'b'.repeat(64), validation_digest: 'c'.repeat(64),
    review_digest: 'e'.repeat(64), repository: 'owner/repo', base_ref: 'main', base_sha: 'f'.repeat(40), title: 'Ship feature',
    body_digest: '1'.repeat(64), runner_mode: 'docker', runner_evidence_digest: '2'.repeat(64), remote_remediation_limit: 3 };
  value.candidate.commit = pr.candidate_commit; value.run.base_sha = pr.base_sha;
  value.candidate.validation_evidence_digest = pr.validation_digest; value.candidate.review_evidence_digest = pr.review_digest;
  vi.mocked(api).mockResolvedValueOnce({ digest: 'd'.repeat(64), text: JSON.stringify(pr) })
    .mockResolvedValueOnce({ digest: scenario === 'body mismatch' ? '9'.repeat(64) : '1'.repeat(64), text: 'Exact PR body' })
    .mockResolvedValueOnce({ token: 'pr-challenge', expires_at: '2099-01-01T00:00:00Z' });
  if (scenario === '409') vi.mocked(api).mockRejectedValueOnce(new ApiError(409, 'stale'));
  else vi.mocked(api).mockResolvedValueOnce({ approval_id: 'approval-1' });
  if (scenario === 'candidate mismatch') value.candidate.commit = '9'.repeat(40);
  render(<RunControls projection={value} onRefresh={vi.fn().mockResolvedValue(value)} />);
  await userEvent.click(screen.getByRole('button', { name: 'Approve PR publication' }));
  if (scenario.endsWith('mismatch')) {
    expect(await screen.findByRole('alert')).toHaveTextContent('changed');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(api).toHaveBeenCalledTimes(scenario === 'body mismatch' ? 2 : 1);
    return;
  }
  const dialog = await screen.findByRole('dialog');
  expect(dialog).toHaveTextContent('Exact PR body');
  expect(dialog).toHaveTextContent('3 remote remediation cycles');
  expect(dialog).toHaveTextContent('This does not authorize merge');
  expect(dialog).toHaveTextContent(pr.runner_evidence_digest);
  await userEvent.click(screen.getByRole('button', { name: 'Confirm approve pr publication' }));
  await waitFor(() => expect(api).toHaveBeenCalledTimes(4));
  expect(JSON.parse(vi.mocked(api).mock.calls[3][1]!.body as string)).toEqual({
    gate: 'pr', run_version: 7, evidence_digest: 'd'.repeat(64), challenge_token: 'pr-challenge',
  });
  if (scenario === '409') {
    expect(await screen.findByRole('alert')).toHaveTextContent('changed');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mutate).not.toHaveBeenCalled();
  }
});

test('renders only commands supplied by the server, regardless of the state label', () => {
  const value = projection({ available_commands: [] });
  render(<RunControls projection={value} onRefresh={vi.fn().mockResolvedValue(value)} />);
  expect(screen.queryByRole('button', { name: 'Approve plan' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Cancel run' })).not.toBeInTheDocument();
});

test('refreshes and binds challenge and approval to exact displayed evidence', async () => {
  const value = projection(); const refresh = vi.fn().mockResolvedValue(value);
  vi.mocked(api).mockResolvedValueOnce({ digest: 'd'.repeat(64), text: JSON.stringify(evidence) })
    .mockResolvedValueOnce({ token: 'one-time-challenge', expires_at: '2099-01-01T00:00:00Z' })
    .mockResolvedValueOnce({ approval_id: 'approved-1' });
  render(<RunControls projection={value} onRefresh={refresh} />);
  await userEvent.click(screen.getByRole('button', { name: 'Approve plan' }));
  await screen.findByRole('dialog');
  expect(screen.getByRole('dialog')).toHaveTextContent('116000');
  expect(refresh).toHaveBeenCalledTimes(1);
  const challenge = JSON.parse(vi.mocked(api).mock.calls[1][1]!.body as string);
  expect(challenge).toEqual({ gate: 'plan', run_version: 7, evidence_digest: 'd'.repeat(64), policy_version: 2 });
  await userEvent.click(screen.getByRole('button', { name: 'Confirm approve plan' }));
  await waitFor(() => expect(api).toHaveBeenCalledTimes(3));
  expect(JSON.parse(vi.mocked(api).mock.calls[2][1]!.body as string)).toEqual({
    gate: 'plan', run_version: 7, evidence_digest: 'd'.repeat(64), challenge_token: 'one-time-challenge',
  });
});

test('409 closes the stale approval and requests authoritative evidence again', async () => {
  const value = projection(); const refresh = vi.fn().mockResolvedValue(value);
  vi.mocked(api).mockResolvedValueOnce({ digest: 'd'.repeat(64), text: JSON.stringify(evidence) })
    .mockResolvedValueOnce({ token: 'challenge', expires_at: '2099-01-01T00:00:00Z' })
    .mockRejectedValueOnce(new ApiError(409, 'stale-projection'));
  render(<RunControls projection={value} onRefresh={refresh} />);
  await userEvent.click(screen.getByRole('button', { name: 'Approve plan' }));
  await screen.findByRole('dialog');
  await userEvent.click(screen.getByRole('button', { name: 'Confirm approve plan' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(screen.getByRole('alert')).toHaveTextContent('changed');
  expect(refresh).toHaveBeenCalledTimes(2);
});

test('cancel requires confirmation and preserves the supplied expected version', async () => {
  const value = projection({ available_commands: [{ name: 'cancel', expected_run_version: 7, requires_feedback: false, gate: null, evidence_digest: null, policy_version: null }] });
  vi.mocked(mutate).mockResolvedValue({ id: 'command-1' });
  render(<RunControls projection={value} onRefresh={vi.fn().mockResolvedValue(value)} />);
  await userEvent.click(screen.getByRole('button', { name: 'Cancel run' }));
  await screen.findByRole('dialog');
  expect(mutate).not.toHaveBeenCalled();
  expect(screen.getByRole('dialog')).toHaveTextContent('resources and evidence remain');
  await userEvent.click(screen.getByRole('button', { name: 'Confirm cancel run' }));
  expect(mutate).toHaveBeenCalledWith('/runs/run-1/commands', { command_type: 'cancel' }, expect.objectContaining({ expectedVersion: 7, idempotencyKey: expect.any(String) }));
});

test('revision requires feedback and retries an uncertain response with the same payload and key', async () => {
  const value = projection({ available_commands: [{ name: 'request_plan_revision', expected_run_version: 7, requires_feedback: true, gate: null, evidence_digest: null, policy_version: null }] });
  vi.mocked(mutate).mockRejectedValueOnce(new Error('connection lost')).mockResolvedValueOnce({ id: 'revision-1' });
  render(<RunControls projection={value} onRefresh={vi.fn().mockResolvedValue(value)} />);
  await userEvent.click(screen.getByRole('button', { name: 'Request revision' }));
  await screen.findByRole('dialog');
  const confirm = screen.getByRole('button', { name: 'Confirm request revision' });
  expect(confirm).toBeDisabled();
  await userEvent.type(screen.getByRole('textbox', { name: 'Revision feedback' }), 'Add migration rollback coverage.');
  await userEvent.click(confirm);
  await screen.findByRole('alert');
  expect(screen.getByRole('textbox')).toHaveAttribute('readonly');
  await userEvent.click(confirm);
  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(2));
  expect(vi.mocked(mutate).mock.calls[1]).toEqual(vi.mocked(mutate).mock.calls[0]);
  expect(vi.mocked(mutate).mock.calls[0][1]).toEqual({ command_type: 'request_plan_revision', feedback: 'Add migration rollback coverage.' });
});

test('changed evidence invalidates an open approval before any submission', async () => {
  const value = projection();
  vi.mocked(api).mockResolvedValueOnce({ digest: 'd'.repeat(64), text: JSON.stringify(evidence) })
    .mockResolvedValueOnce({ token: 'challenge', expires_at: '2099-01-01T00:00:00Z' });
  const refresh = vi.fn().mockResolvedValue(value);
  const view = render(<RunControls projection={value} onRefresh={refresh} />);
  await userEvent.click(screen.getByRole('button', { name: 'Approve plan' }));
  await screen.findByRole('dialog');
  const changed = structuredClone(value); changed.available_commands[0].evidence_digest = 'e'.repeat(64);
  view.rerender(<RunControls projection={changed} onRefresh={refresh} />);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('changed');
  expect(api).toHaveBeenCalledTimes(2);
});
