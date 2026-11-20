import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import DirectPage from './page';
import DirectDetailPage from './[id]/page';
import { api, mutate } from '@/lib/api/client';

const push = vi.fn();
vi.mock('next/navigation', () => ({ useRouter: () => ({ push }), useParams: () => ({ id: 'run-1' }) }));
vi.mock('@/lib/api/client', () => ({ api: vi.fn(), mutate: vi.fn() }));

const run = {
  control_version: 7, approval_task_id: null,
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
      shape: 'quick', review_plan_first: false,
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

test('detail polls the run from RUNNING to DONE and loads terminal artifacts', async () => {
  vi.useFakeTimers();
  try {
    let runCalls = 0;
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === '/direct-runs/run-1') {
        runCalls += 1;
        return (runCalls === 1 ? run : { ...run, state: 'DONE', summary: 'All fixed', head_sha: 'b'.repeat(40) }) as T;
      }
      if (path === '/direct-runs/run-1/transcript') return { text: 'agent output line' } as T;
      if (path === '/direct-runs/run-1/diff') return { diff: '+changed' } as T;
      throw new Error(`unexpected path ${path}`);
    });
    render(<DirectDetailPage />);
    await act(async () => {});
    expect(screen.getByText(/State: RUNNING/)).toBeInTheDocument();
    expect(screen.queryByText('+changed')).not.toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(3000); });
    await act(async () => {});
    expect(screen.getByText(/State: DONE/)).toBeInTheDocument();
    expect(screen.getByText('All fixed')).toBeInTheDocument();
    expect(screen.getByText('+changed')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel run' })).not.toBeInTheDocument();
    expect(runCalls).toBeGreaterThanOrEqual(2);
  } finally {
    vi.useRealTimers();
  }
});

test('creates a planned run with plan review from the form', async () => {
  mockApi();
  vi.mocked(mutate).mockResolvedValue(run);
  render(<DirectPage />);
  await screen.findByText(/Fix the parser bug/);
  await userEvent.selectOptions(screen.getByLabelText('Shape'), 'planned');
  await userEvent.click(screen.getByLabelText(/Review plan before implementation/));
  await userEvent.type(screen.getByLabelText('Task'), 'Plan then fix');
  await userEvent.click(screen.getByRole('button', { name: 'Start direct run' }));
  expect(vi.mocked(mutate)).toHaveBeenCalledWith(
    '/direct-runs',
    expect.objectContaining({ shape: 'planned', review_plan_first: true }),
    expect.objectContaining({ idempotencyKey: expect.any(String) }),
  );
});

test('detail shows the plan and approves it', async () => {
  mockApi({
    '/direct-runs/run-1': {
      ...run, state: 'AWAITING_PLAN_APPROVAL', shape: 'planned',
      review_plan_first: true, plan_text: 'Step 1: edit parser.py', plan_approved: false,
    },
  });
  vi.mocked(mutate).mockResolvedValue({ ...run, state: 'QUEUED', plan_approved: true });
  render(<DirectDetailPage />);
  await screen.findByText(/State: AWAITING_PLAN_APPROVAL/);
  await screen.findByText(/Step 1: edit parser\.py/);
  await userEvent.click(screen.getByRole('button', { name: 'Approve plan and continue' }));
  expect(vi.mocked(mutate)).toHaveBeenCalledWith(
    '/direct-runs/run-1/approve-plan', { expected_version: 7, task_id: null }, expect.objectContaining({ idempotencyKey: expect.any(String) }),
  );
  expect(await screen.findByRole('button', { name: 'Cancel run' })).toBeInTheDocument();
});

test('creates an epic run with decomposition review from the form', async () => {
  mockApi();
  vi.mocked(mutate).mockResolvedValue(run);
  render(<DirectPage />);
  await screen.findByText(/Fix the parser bug/);
  await userEvent.selectOptions(screen.getByLabelText('Shape'), 'epic');
  await userEvent.click(screen.getByLabelText(/Review decomposition and child plans before implementation/));
  await userEvent.type(screen.getByLabelText('Task'), 'Build the big feature');
  await userEvent.click(screen.getByRole('button', { name: 'Start direct run' }));
  expect(vi.mocked(mutate)).toHaveBeenCalledWith(
    '/direct-runs',
    expect.objectContaining({ shape: 'epic', review_plan_first: true }),
    expect.objectContaining({ idempotencyKey: expect.any(String) }),
  );
});

test('detail shows epic tasks, reorders pending tasks and approves decomposition', async () => {
  const tasks = [
    { id: 'task-1', run_id: 'run-1', ordinal: 0, task: 'first step', shape: 'quick', state: 'PENDING', plan_text: null, head_sha: null, summary: null, created_at: null, updated_at: null },
    { id: 'task-2', run_id: 'run-1', ordinal: 1, task: 'second step', shape: 'planned', state: 'PENDING', plan_text: null, head_sha: null, summary: null, created_at: null, updated_at: null },
  ];
  mockApi({ '/direct-runs/run-1': { ...run, state: 'AWAITING_DECOMPOSITION_APPROVAL', shape: 'epic', review_plan_first: true, plan_approved: false, tasks } });
  vi.mocked(mutate).mockResolvedValue({ ...run, state: 'QUEUED' });
  render(<DirectDetailPage />);
  await screen.findByText(/State: AWAITING_DECOMPOSITION_APPROVAL/);
  await screen.findByText('Epic tasks');
  await userEvent.click(screen.getByRole('button', { name: 'Move task 2 up' }));
  await userEvent.click(screen.getByRole('button', { name: 'Save task order' }));
  expect(vi.mocked(mutate)).toHaveBeenCalledWith(
    '/direct-runs/run-1/tasks',
    { expected_version: 7, tasks: [
      { id: 'task-2', task: 'second step', shape: 'planned' },
      { id: 'task-1', task: 'first step', shape: 'quick' },
    ] },
    expect.objectContaining({ idempotencyKey: expect.any(String), method: 'PUT' }),
  );
  await userEvent.click(await screen.findByRole('button', { name: 'Approve decomposition and continue' }));
  expect(vi.mocked(mutate)).toHaveBeenCalledWith(
    '/direct-runs/run-1/approve-decomposition', { expected_version: 7 }, expect.objectContaining({ idempotencyKey: expect.any(String) }),
  );
});

test('detail marks the current epic child while running', async () => {
  const tasks = [
    { id: 'task-1', run_id: 'run-1', ordinal: 0, task: 'first step', shape: 'quick', state: 'DONE', plan_text: null, head_sha: 'abc', summary: 'did one', created_at: null, updated_at: null },
    { id: 'task-2', run_id: 'run-1', ordinal: 1, task: 'second step', shape: 'planned', state: 'RUNNING', plan_text: 'plan two', head_sha: null, summary: null, created_at: null, updated_at: null },
  ];
  mockApi({ '/direct-runs/run-1': { ...run, state: 'RUNNING', shape: 'epic', tasks } });
  render(<DirectDetailPage />);
  await screen.findByText(/State: RUNNING/);
  await screen.findByText(/second step — RUNNING \(current\)/);
  await screen.findByText('did one');
});

test('epic task edits survive failed saves and keep their original snapshot', async () => {
  const task = { id: 'task-1', ordinal: 0, task: 'first step', shape: 'quick', state: 'PENDING' };
  mockApi({ '/direct-runs/run-1': { ...run, state: 'AWAITING_DECOMPOSITION_APPROVAL', shape: 'epic', tasks: [task] } });
  vi.mocked(mutate).mockRejectedValue(new Error('stale'));
  render(<DirectDetailPage />);
  const input = await screen.findByLabelText('Task 1 text');
  await userEvent.clear(input);
  await userEvent.type(input, 'owner revision');
  await userEvent.click(screen.getByRole('button', { name: 'Save task order' }));
  await screen.findByRole('alert');
  expect(input).toHaveValue('owner revision');
  expect(screen.getByRole('button', { name: 'Approve decomposition and continue' })).toBeDisabled();
  expect(vi.mocked(mutate)).toHaveBeenCalledWith('/direct-runs/run-1/tasks', {
    expected_version: 7, tasks: [{ id: 'task-1', task: 'owner revision', shape: 'quick' }],
  }, expect.objectContaining({ method: 'PUT' }));
});

test('owner refreshes stale task edits and explicitly applies retained work to the inspected snapshot', async () => {
  const tasks = [
    { id: 'task-1', ordinal: 0, task: 'first step', shape: 'quick', state: 'PENDING' },
    { id: 'task-2', ordinal: 1, task: 'second step', shape: 'planned', state: 'PENDING' },
  ];
  const responses = { '/direct-runs/run-1': { ...run, state: 'AWAITING_DECOMPOSITION_APPROVAL', shape: 'epic', tasks } };
  mockApi(responses);
  vi.mocked(mutate).mockRejectedValueOnce(new Error('stale')).mockResolvedValue({});
  render(<DirectDetailPage />);
  await userEvent.click(await screen.findByRole('button', { name: 'Move task 2 up' }));
  await userEvent.clear(screen.getByLabelText('Task 1 text'));
  await userEvent.type(screen.getByLabelText('Task 1 text'), 'retained owner text');
  await userEvent.click(screen.getByRole('button', { name: 'Save task order' }));
  await screen.findByRole('alert');
  responses['/direct-runs/run-1'] = { ...responses['/direct-runs/run-1'], control_version: 8,
    tasks: tasks.map(t => t.id === 'task-1' ? { ...t, task: 'server revision' } : t) };
  await userEvent.click(screen.getByRole('button', { name: 'Refresh run' }));
  await screen.findByText('Latest saved tasks (snapshot 8)');
  await screen.findByText(/server revision/);
  expect(screen.getByLabelText('Task 1 text')).toHaveValue('retained owner text');
  expect(screen.getByLabelText('Task 2 text')).toHaveValue('first step');
  expect(screen.getByRole('button', { name: 'Save task order' })).toBeDisabled();
  expect(vi.mocked(mutate)).toHaveBeenCalledTimes(1);
  await userEvent.click(screen.getByRole('button', { name: 'Apply retained edits to snapshot 8' }));
  expect(vi.mocked(mutate)).toHaveBeenLastCalledWith('/direct-runs/run-1/tasks', {
    expected_version: 8, tasks: [
      { id: 'task-2', task: 'retained owner text', shape: 'planned' },
      { id: 'task-1', task: 'first step', shape: 'quick' },
    ],
  }, expect.objectContaining({ method: 'PUT' }));
});

test.each(['RUNNING', 'CANCELLED'])('refresh retains copyable edits when another tab moves the run to %s', async state => {
  const responses = { '/direct-runs/run-1': { ...run, state: 'AWAITING_DECOMPOSITION_APPROVAL', shape: 'epic', tasks: [
    { id: 'task-1', ordinal: 0, task: 'first step', shape: 'quick', state: 'PENDING' },
  ] } };
  mockApi(responses);
  render(<DirectDetailPage />);
  const input = await screen.findByLabelText('Task 1 text');
  await userEvent.clear(input);
  await userEvent.type(input, 'unsaved owner work');
  responses['/direct-runs/run-1'] = { ...responses['/direct-runs/run-1'], state, control_version: 8 };
  await userEvent.click(screen.getByRole('button', { name: 'Refresh run' }));
  const retained = await screen.findByLabelText('Retained task edits');
  expect(retained).toHaveValue(JSON.stringify([{ id: 'task-1', task: 'unsaved owner work', shape: 'quick' }], null, 2));
  expect(retained).toHaveAttribute('readonly');
  expect(screen.queryByRole('button', { name: /Apply retained edits/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Save task order' })).not.toBeInTheDocument();
  expect(vi.mocked(mutate)).not.toHaveBeenCalled();
});

test('owner uses the current task list after retained IDs are replaced without remapping the draft', async () => {
  const responses = { '/direct-runs/run-1': { ...run, state: 'AWAITING_DECOMPOSITION_APPROVAL', shape: 'epic', tasks: [
    { id: 'removed-task', ordinal: 0, task: 'old task', shape: 'quick', state: 'PENDING' },
  ] } };
  mockApi(responses);
  vi.mocked(mutate).mockRejectedValue(new Error('unknown task identity'));
  render(<DirectDetailPage />);
  await userEvent.type(await screen.findByLabelText('Task 1 text'), ' retained edit');
  responses['/direct-runs/run-1'] = { ...responses['/direct-runs/run-1'], control_version: 8, tasks: [
    { id: 'replacement-task', ordinal: 0, task: 'current replacement', shape: 'quick', state: 'PENDING' },
  ] };
  await userEvent.click(screen.getByRole('button', { name: 'Refresh run' }));
  await screen.findByText('Latest saved tasks (snapshot 8)');
  expect(await screen.findByLabelText('Retained task edits')).toHaveValue(JSON.stringify([
    { id: 'removed-task', task: 'old task retained edit', shape: 'quick' },
  ], null, 2));
  await userEvent.click(screen.getByRole('button', { name: 'Apply retained edits to snapshot 8' }));
  await screen.findByRole('alert');
  expect(vi.mocked(mutate)).toHaveBeenLastCalledWith('/direct-runs/run-1/tasks', {
    expected_version: 8, tasks: [{ id: 'removed-task', task: 'old task retained edit', shape: 'quick' }],
  }, expect.objectContaining({ method: 'PUT' }));
  await userEvent.click(screen.getByRole('button', { name: 'Use current task list' }));
  expect(screen.getByLabelText('Task 1 text')).toHaveValue('current replacement');
  expect(screen.queryByLabelText('Retained task edits')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Approve decomposition and continue' })).toBeEnabled();
  expect(vi.mocked(mutate)).toHaveBeenCalledTimes(1);
  vi.mocked(mutate).mockResolvedValueOnce({});
  await userEvent.click(screen.getByRole('button', { name: 'Save task order' }));
  expect(vi.mocked(mutate)).toHaveBeenLastCalledWith('/direct-runs/run-1/tasks', {
    expected_version: 8, tasks: [{ id: 'replacement-task', task: 'current replacement', shape: 'quick' }],
  }, expect.objectContaining({ method: 'PUT' }));
});

test('failed refresh preserves retained edits and disables recovery writes until a current snapshot loads', async () => {
  mockApi({ '/direct-runs/run-1': { ...run, state: 'AWAITING_DECOMPOSITION_APPROVAL', shape: 'epic', tasks: [
    { id: 'task-1', ordinal: 0, task: 'old task', shape: 'quick', state: 'PENDING' },
  ] } });
  render(<DirectDetailPage />);
  await userEvent.type(await screen.findByLabelText('Task 1 text'), ' retained');
  vi.mocked(api).mockRejectedValueOnce(new Error('offline'));
  await userEvent.click(screen.getByRole('button', { name: 'Refresh run' }));
  await screen.findByText(/Refresh failed/);
  expect(screen.getByLabelText('Retained task edits')).toHaveValue(JSON.stringify([
    { id: 'task-1', task: 'old task retained', shape: 'quick' },
  ], null, 2));
  expect(screen.getByRole('button', { name: 'Use current task list' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Save task order' })).toBeDisabled();
  expect(vi.mocked(mutate)).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: 'Refresh run' }));
  expect(await screen.findByRole('button', { name: 'Use current task list' })).toBeEnabled();
});

test('epic child approval carries the specific child and current snapshot', async () => {
  mockApi({ '/direct-runs/run-1': { ...run, shape: 'epic', state: 'AWAITING_PLAN_APPROVAL', approval_task_id: 'task-2', tasks: [
    { id: 'task-1', ordinal: 0, task: 'first', shape: 'quick', state: 'DONE' },
    { id: 'task-2', ordinal: 1, task: 'second', shape: 'planned', state: 'AWAITING_PLAN_APPROVAL', plan_text: 'second plan' },
  ] } });
  vi.mocked(mutate).mockResolvedValue({});
  render(<DirectDetailPage />);
  await userEvent.click(await screen.findByRole('button', { name: 'Approve task plan and continue' }));
  expect(vi.mocked(mutate)).toHaveBeenCalledWith('/direct-runs/run-1/approve-plan', {
    expected_version: 7, task_id: 'task-2',
  }, expect.objectContaining({ idempotencyKey: expect.any(String) }));
});

test('detail shows diff and hides cancel when done', async () => {
  mockApi({ '/direct-runs/run-1': { ...run, state: 'DONE', summary: 'All fixed', head_sha: 'b'.repeat(40) } });
  render(<DirectDetailPage />);
  await screen.findByText(/State: DONE/);
  await screen.findByText('+changed');
  expect(screen.queryByRole('button', { name: 'Cancel run' })).not.toBeInTheDocument();
});


