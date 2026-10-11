import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import ProjectPage from './page';

const projectId = '11111111-1111-4111-8111-111111111111';
vi.mock('next/navigation', () => ({ useParams: () => ({ projectId }) }));

const policy = {
  project_id: projectId, revision: 2, mode: 'checkpoint', warning_multiplier: 1.5,
  checkpoint_multiplier: 2, history_limit: 50, minimum_comparables: 20,
  window_days: null, hard_token_cap: 500000, hard_context_cap: null,
  manual_estimate_tokens: null, allow_cross_project_fallback: true,
};

function response(value: unknown, status = 200) {
  return Response.json(value, { status });
}

function installHttp({ initial = policy, firstPostConflict = false, deferProjectRefresh = false }: { initial?: typeof policy | null; firstPostConflict?: boolean; deferProjectRefresh?: boolean } = {}) {
  let refreshCount = 0;
  let postCount = 0;
  let projectReadCount = 0;
  let completeRefresh!: (result: Response) => void;
  let failRefresh!: (cause: Error) => void;
  const pendingRefresh = new Promise<Response>((resolve, reject) => {
    completeRefresh = resolve;
    failRefresh = reject;
  });
  let completeProjectRefresh!: (result: Response) => void;
  const pendingProjectRefresh = new Promise<Response>(resolve => { completeProjectRefresh = resolve; });
  const requests: Array<{ url: string; init: RequestInit }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    requests.push({ url, init });
    if (url === `/api/projects/${projectId}/monitoring-policy`) {
      if (init.method === 'POST') {
        postCount++;
        if (firstPostConflict && postCount === 1) return response({ detail: 'stale-project-policy' }, 409);
        return response({ ...policy, revision: firstPostConflict ? 9 : initial ? 3 : 1 });
      }
      if (init.cache === 'no-store') {
        refreshCount++;
        return refreshCount === 1 ? response(initial) : refreshCount === 2 ? pendingRefresh : response({ ...policy, revision: firstPostConflict ? 8 : 3 });
      }
      return response({ ...policy, revision: 8 });
    }
    if (url === `/api/projects/${projectId}`) {
      projectReadCount++;
      if (deferProjectRefresh && projectReadCount === 2) return pendingProjectRefresh;
      return response({ id: projectId, name: 'Project', repository_path: 'C:/repo', github_repository: 'owner/repo', default_branch: 'main', policy_version: 0 });
    }
    if (url === '/api/subscription-profiles') return response([]);
    if (url === `/api/projects/${projectId}/subscription-profile`) return response(null);
    if (url.startsWith('/api/task-usage-report?')) return response(null);
    throw new Error(`Unexpected request ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { requests, completeRefresh, failRefresh, completeProjectRefresh };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test('keeps edited caps and note through 409, actual-revision fetch, failed refresh and retry', async () => {
  const http = installHttp({ firstPostConflict: true });
  render(<ProjectPage />);
  await screen.findByRole('form', { name: 'Revise task usage monitoring policy' });
  await userEvent.click(screen.getByText(/Advanced hard caps and manual estimates/i));
  const cap = screen.getByRole('textbox', { name: 'Optional hard token cap' });
  await userEvent.clear(cap);
  await userEvent.type(cap, '750000');
  await userEvent.type(screen.getByRole('textbox', { name: 'Optional hard context cap' }), '900000');
  const warning = screen.getByRole('spinbutton', { name: 'Warning multiplier' });
  await userEvent.clear(warning);
  await userEvent.type(warning, '1.8');
  await userEvent.type(screen.getByRole('textbox', { name: 'Optional owner note' }), 'Keep draft');
  await userEvent.click(screen.getByRole('button', { name: 'Save monitoring policy' }));
  expect(await screen.findByText(/Monitoring policy revision conflict/i)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Load current revision and keep edits' }));
  await waitFor(() => expect(http.requests.filter(r => r.url === `/api/projects/${projectId}/monitoring-policy` && r.init.cache === 'no-store')).toHaveLength(2));
  expect(screen.getByRole('textbox', { name: 'Optional hard token cap' })).toHaveValue('750000');
  expect(screen.getByRole('textbox', { name: 'Optional hard context cap' })).toHaveValue('900000');
  expect(screen.getByRole('spinbutton', { name: 'Warning multiplier' })).toHaveValue(1.8);
  expect(screen.getByRole('textbox', { name: 'Optional owner note' })).toHaveValue('Keep draft');
  expect(screen.getByRole('heading', { name: /Revision 8/i })).toBeInTheDocument();
  http.failRefresh(new Error('refresh unavailable'));
  expect(await screen.findByText(/Monitoring policy unavailable/i)).toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: 'Optional owner note' })).toHaveValue('Keep draft');
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
  await waitFor(() => expect(http.requests.filter(r => r.url === `/api/projects/${projectId}/monitoring-policy` && r.init.cache === 'no-store')).toHaveLength(3));
  expect(screen.getByRole('textbox', { name: 'Optional owner note' })).toHaveValue('Keep draft');
  await userEvent.click(screen.getByRole('button', { name: 'Save monitoring policy' }));
  const posts = http.requests.filter(r => r.url === `/api/projects/${projectId}/monitoring-policy` && r.init.method === 'POST');
  expect(posts).toHaveLength(2);
  expect(JSON.parse(posts[1].init.body as string)).toEqual(expect.objectContaining({ expected_revision: 8, hard_token_cap: 750000, hard_context_cap: 900000, warning_multiplier: 1.8, note: 'Keep draft' }));
});

test('keeps draft and returned revision across ordinary post-save refresh', async () => {
  const http = installHttp();
  render(<ProjectPage />);
  await screen.findByRole('form', { name: 'Revise task usage monitoring policy' });
  await userEvent.type(screen.getByRole('textbox', { name: 'Optional owner note' }), 'Continue editing');
  await userEvent.click(screen.getByRole('button', { name: 'Save monitoring policy' }));
  await waitFor(() => expect(http.requests.filter(r => r.url === `/api/projects/${projectId}/monitoring-policy` && r.init.cache === 'no-store')).toHaveLength(2));
  expect(screen.getByRole('textbox', { name: 'Optional owner note' })).toHaveValue('Continue editing');
  expect(screen.getByRole('heading', { name: /Revision 3/i })).toBeInTheDocument();
  http.completeRefresh(response({ ...policy, revision: 3 }));
  await userEvent.click(screen.getByRole('button', { name: 'Save monitoring policy' }));
  const posts = http.requests.filter(r => r.url === `/api/projects/${projectId}/monitoring-policy` && r.init.method === 'POST');
  expect(JSON.parse(posts[1].init.body as string).expected_revision).toBe(3);
});

test('allows first policy creation from absent legacy policy at revision zero', async () => {
  const http = installHttp({ initial: null });
  render(<ProjectPage />);
  await screen.findByRole('form', { name: 'Revise task usage monitoring policy' });
  await userEvent.click(screen.getByRole('button', { name: 'Save monitoring policy' }));
  const post = http.requests.find(r => r.url === `/api/projects/${projectId}/monitoring-policy` && r.init.method === 'POST');
  expect(JSON.parse(post!.init.body as string).expected_revision).toBe(0);
});

test('retains monitoring draft while the outer project projection refreshes', async () => {
  const http = installHttp({ deferProjectRefresh: true });
  render(<ProjectPage />);
  await screen.findByRole('form', { name: 'Revise task usage monitoring policy' });
  await userEvent.type(screen.getByRole('textbox', { name: 'Optional owner note' }), 'Unsent owner draft');
  await userEvent.click(screen.getByRole('button', { name: 'Reload current policy' }));
  await waitFor(() => expect(http.requests.filter(r => r.url === `/api/projects/${projectId}`)).toHaveLength(2));
  expect(screen.getByRole('textbox', { name: 'Optional owner note' })).toHaveValue('Unsent owner draft');
  http.completeProjectRefresh(response({ id: projectId, name: 'Project', repository_path: 'C:/repo', github_repository: 'owner/repo', default_branch: 'main', policy_version: 0 }));
});
