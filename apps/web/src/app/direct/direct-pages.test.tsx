import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import DirectPage from './page';
import DirectDetailPage from './[id]/page';
import { api, mutate } from '@/lib/api/client';

const push = vi.fn();
vi.mock('next/navigation', () => ({ useRouter: () => ({ push }), useParams: () => ({ id: 'run-1' }) }));
vi.mock('@/lib/api/client', () => ({ api: vi.fn(), mutate: vi.fn() }));

const run = {
  id: 'run-1', project_id: 'project-1', task: 'Fix the parser bug',
  client: 'antigravity_cli', model: 'gemini-3.8-flash-medium', effort: 'medium',
  state: 'RUNNING', branch: 'forge/direct-run1', worktree_path: 'C:/wt/run-1',
  base_sha: 'a'.repeat(40), head_sha: null, summary: null, question: null,
  options: [], failure_reason: null, result_degraded: false,
  cancel_requested: false, created_at: '2026-10-22T10:00:00Z',
  updated_at: '2026-10-22T10:01:00Z', sessions: [], checks: [], reviews: [],
};

afterEach(() => {
  cleanup();
  vi.mocked(api).mockReset();
  vi.mocked(mutate).mockReset();
  push.mockReset();
});

function mockApi(overrides: Record<string, unknown> = {}) {
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path in overrides) return overrides[path] as T;
    if (path === '/direct-runs') return [run] as T;
    if (path === '/projects') return [{ id: 'project-1', name: 'Forge' }] as T;
    if (path === '/direct-runs/routes') {
      return [{ client: 'antigravity_cli', model: 'gemini-3.8-flash-medium', effort: 'medium' }] as T;
    }
    if (path === '/direct-runs/run-1') return run as T;
    if (path === '/direct-runs/run-1/transcript') return { text: 'agent output line' } as T;
    if (path === '/direct-runs/run-1/diff') return { diff: '+changed' } as T;
    throw new Error(`unexpected path ${path}`);
  });
}

test('creates a direct run from the form and navigates to it', async () => {
  mockApi();
  vi.mocked(mutate).mockResolvedValue(run);
  render(<DirectPage />);
  await screen.findByText(/Fix the parser bug/);
  await userEvent.type(screen.getByLabelText('Task'), 'Fix the parser bug');
  await userEvent.click(screen.getByRole('button', { name: 'Start direct run' }));
  expect(vi.mocked(mutate)).toHaveBeenCalledWith(
    '/direct-runs',
    {
      project_id: 'project-1', task: 'Fix the parser bug',
      client: 'antigravity_cli', model: 'gemini-3.8-flash-medium', effort: 'medium',
    },
    expect.objectContaining({ idempotencyKey: expect.any(String) }),
  );
  expect(push).toHaveBeenCalledWith('/direct/run-1');
});

test('detail shows live transcript and cancels a running run', async () => {
  mockApi();
  vi.mocked(mutate).mockResolvedValue({ ...run, cancel_requested: true });
  render(<DirectDetailPage />);
  await screen.findByText(/State: RUNNING/);
  await screen.findByText('agent output line');
  await userEvent.click(screen.getByRole('button', { name: 'Cancel run' }));
  expect(vi.mocked(mutate)).toHaveBeenCalledWith(
    '/direct-runs/run-1/cancel', {}, expect.objectContaining({ idempotencyKey: expect.any(String) }),
  );
});

test('detail shows checks with excerpt and checking state', async () => {
  mockApi({
    '/direct-runs/run-1': {
      ...run, state: 'CHECKING',
      checks: [{
        id: 'check-1', run_id: 'run-1', round: 0, command_name: 'smoke', kind: 'test',
        tree_sha: 'a'.repeat(40), command_digest: 'b'.repeat(64), policy_version: 1,
        status: 'FAILED', exit_code: 1, duration_ms: 12,
        failure_excerpt: 'FAILED tests/test_smoke.py::test_it', created_at: '2026-10-22T10:02:00Z',
      }],
    },
  });
  render(<DirectDetailPage />);
  await screen.findByText(/State: CHECKING/);
  await screen.findByText(/Round 0: smoke \(test\) — FAILED/);
  await screen.findByText(/FAILED tests\/test_smoke\.py::test_it/);
});

test('detail shows reviews section', async () => {
  mockApi({
    '/direct-runs/run-1': {
      ...run, state: 'REVIEWING',
      reviews: [{
        id: 'rev-1', run_id: 'run-1', session_id: 's-1', round: 0,
        client: 'codex_app_server', model: 'review-model', verdict: 'blocking',
        findings: [{ severity: 'blocking', file: 'a.py', detail: 'fix this bug' }],
        summary: 'needs work', degraded: false, reviewer_modified_worktree: false,
        created_at: '2026-10-22T10:03:00Z',
      }],
    },
  });
  render(<DirectDetailPage />);
  await screen.findByText(/State: REVIEWING/);
  await screen.findByText(/Round 0: codex_app_server\/review-model — blocking/);
  await screen.findByText(/fix this bug/);
  await screen.findByText('needs work');
});

test('detail shows pull request link and rounds', async () => {
  mockApi({ '/direct-runs/run-1': { ...run, state: 'PR_OPEN', pr_number: 42, pr_url: 'https://example.test/pr/42', pr_rounds: 1 } });
  render(<DirectDetailPage />);
  await screen.findByText(/State: PR_OPEN/);
  const link = await screen.findByRole('link', { name: 'PR #42' });
  expect(link).toHaveAttribute('href', 'https://example.test/pr/42');
  await screen.findByText(/PR rounds: 1/);
});

test('detail shows diff and hides cancel when done', async () => {
  mockApi({ '/direct-runs/run-1': { ...run, state: 'DONE', summary: 'All fixed', head_sha: 'b'.repeat(40) } });
  render(<DirectDetailPage />);
  await screen.findByText(/State: DONE/);
  await screen.findByText('+changed');
  expect(screen.queryByRole('button', { name: 'Cancel run' })).not.toBeInTheDocument();
});


