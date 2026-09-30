import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { NewRunForm } from './new-run-form';
import { api, ApiError, mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';

vi.mock('@/lib/api/client', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/api/client')>(), mutate: vi.fn(), api: vi.fn(),
}));
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); vi.mocked(mutate).mockReset(); });
beforeEach(() => { vi.mocked(api).mockResolvedValue([]); });
const project: components['schemas']['ProjectResponse'] = {
  id: 'project-1', name: 'Parallel', repository_path: 'C:/code/parallel',
  canonical_path_key: 'c:/code/parallel', github_repository: 'owner/repo', default_branch: 'main',
  instructions_path: null, policy_version: 1, policy_digest: 'a'.repeat(64),
  issue_import_available: false,
  policy: { runner_mode: 'docker', commands: [], planner_model: { provider: 'google', model: 'test-model' } },
};

async function fill() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText('Task title'), 'Build feature');
  await user.type(screen.getByLabelText('Task description'), 'A concrete change');
  return user;
}

test('creates a plain task then a run and navigates only after the run exists', async () => {
  vi.mocked(mutate).mockReset().mockResolvedValueOnce({ id: 'task-1' }).mockResolvedValueOnce({ id: 'run-1' });
  const navigate = vi.fn();
  render(<NewRunForm projects={[project]} onCreated={navigate} />);
  const user = await fill();
  expect(screen.queryByLabelText(/issue/i)).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Create run' }));
  await waitFor(() => expect(navigate).toHaveBeenCalledWith('run-1'));
  expect(mutate).toHaveBeenNthCalledWith(1, '/tasks', {
    project_id: 'project-1', title: 'Build feature', body: 'A concrete change',
  }, expect.objectContaining({ idempotencyKey: expect.any(String) }));
  expect(mutate).toHaveBeenNthCalledWith(2, '/runs', { task_id: 'task-1' }, expect.any(Object));
});

test('retries an uncertain run response using the same task and run key', async () => {
  vi.mocked(mutate).mockReset().mockResolvedValueOnce({ id: 'task-1' })
    .mockRejectedValueOnce(new Error('private network detail')).mockResolvedValueOnce({ id: 'run-1' });
  render(<NewRunForm projects={[project]} onCreated={vi.fn()} />);
  const user = await fill();
  await user.click(screen.getByRole('button', { name: 'Create run' }));
  expect(await screen.findByRole('alert')).not.toHaveTextContent('private network detail');
  expect(screen.getByLabelText('Subscription profile')).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Retry creation' }));
  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(3));
  expect(vi.mocked(mutate).mock.calls[2]).toEqual(vi.mocked(mutate).mock.calls[1]);
  expect(screen.getByLabelText('Task title')).toHaveValue('Build feature');
});

test('captures a chosen immutable profile in the run request and permits correction after definitive rejection', async () => {
  vi.mocked(api).mockResolvedValueOnce([{
    profile_id: 'profile-1', version: 3, preferences: [{ purpose: 'primary', preferred_route: { provider: 'openai', model: 'gpt-test' } }],
    approved_mappings: [], default_billing_mode: 'allowance_only', jev: null,
  }]);
  vi.mocked(mutate).mockReset().mockResolvedValueOnce({ id: 'task-1' })
    .mockRejectedValueOnce(new ApiError(422, 'request-failed'))
    .mockRejectedValueOnce(new Error('lost corrected response'))
    .mockResolvedValueOnce({ id: 'run-1' });
  render(<NewRunForm projects={[{ ...project, issue_import_available: true }, { ...project, id: 'project-2', name: 'Second' }]} onCreated={vi.fn()} />);
  await screen.findByRole('option', { name: /profile-1/ });
  const user = await fill();
  await user.selectOptions(screen.getByLabelText('Task source'), 'text');
  await user.selectOptions(screen.getByLabelText('Subscription profile'), 'profile-1:3');
  await user.click(screen.getByRole('button', { name: 'Create run' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('Project')).toBeDisabled();
  expect(screen.getByLabelText('Task source')).toBeDisabled();
  expect(screen.getByLabelText('Task title')).toHaveAttribute('readonly');
  expect(screen.getByLabelText('Task description')).toHaveAttribute('readonly');
  expect(screen.getByLabelText('Subscription profile')).toBeEnabled();
  expect(screen.getByLabelText('Project')).toHaveValue('project-1');
  expect(screen.getByLabelText('Task title')).toHaveValue('Build feature');
  expect(screen.getByLabelText('Task description')).toHaveValue('A concrete change');
  expect(vi.mocked(mutate).mock.calls[1][1]).toEqual({ task_id: 'task-1', profile_id: 'profile-1', profile_version: 3 });
  await user.selectOptions(screen.getByLabelText('Subscription profile'), 'default');
  await user.click(screen.getByRole('button', { name: 'Retry creation' }));
  await waitFor(() => expect(vi.mocked(mutate)).toHaveBeenCalledTimes(3));
  expect(await screen.findByRole('alert')).toHaveTextContent('Creation could not be confirmed');
  expect(screen.getByLabelText('Subscription profile')).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Retry creation' }));
  await waitFor(() => expect(vi.mocked(mutate)).toHaveBeenCalledTimes(4));
  expect(vi.mocked(mutate).mock.calls[2][1]).toEqual({ task_id: 'task-1' });
  expect(vi.mocked(mutate).mock.calls[3]).toEqual(vi.mocked(mutate).mock.calls[2]);
  expect(vi.mocked(mutate).mock.calls[2][2].idempotencyKey).not.toBe(vi.mocked(mutate).mock.calls[1][2].idempotencyKey);
  expect(vi.mocked(mutate).mock.calls.filter(call => call[0] === '/tasks')).toHaveLength(1);
});

test('an uncertain task response retains its request key and prevents duplicate clicks', async () => {
  let reject!: (error: Error) => void;
  vi.mocked(mutate).mockReset().mockImplementationOnce(() => new Promise((_, fail) => { reject = fail; }))
    .mockResolvedValueOnce({ id: 'task-1' }).mockResolvedValueOnce({ id: 'run-1' });
  render(<NewRunForm projects={[project]} onCreated={vi.fn()} />);
  const user = await fill();
  await user.dblClick(screen.getByRole('button', { name: 'Create run' }));
  expect(mutate).toHaveBeenCalledTimes(1);
  await act(async () => reject(new Error('timeout')));
  await screen.findByRole('alert');
  await user.click(screen.getByRole('button', { name: 'Retry creation' }));
  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(3));
  expect(vi.mocked(mutate).mock.calls[1]).toEqual(vi.mocked(mutate).mock.calls[0]);
});

test.each([404, 409, 500])('a definitive-looking HTTP %i run failure stays frozen and retries the identical default request', async status => {
  vi.mocked(mutate).mockReset().mockResolvedValueOnce({ id: 'task-1' })
    .mockRejectedValueOnce(new ApiError(status, 'request-failed'))
    .mockResolvedValueOnce({ id: 'run-1' });
  render(<NewRunForm projects={[project]} onCreated={vi.fn()} />);
  const user = await fill();
  await user.click(screen.getByRole('button', { name: 'Create run' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('Subscription profile')).toBeDisabled();
  expect(screen.getByLabelText('Project')).toBeDisabled();
  expect(screen.getByLabelText('Task title')).toHaveAttribute('readonly');
  await user.click(screen.getByRole('button', { name: 'Retry creation' }));
  await waitFor(() => expect(vi.mocked(mutate)).toHaveBeenCalledTimes(3));
  expect(vi.mocked(mutate).mock.calls[2]).toEqual(vi.mocked(mutate).mock.calls[1]);
});

test('choosing a profile then changing projects resets to the new project default', async () => {
  vi.mocked(api).mockResolvedValueOnce([{
    profile_id: 'profile-1', version: 3, preferences: [], approved_mappings: [], default_billing_mode: 'allowance_only', jev: null,
  }]);
  vi.mocked(mutate).mockReset().mockResolvedValueOnce({ id: 'task-2' }).mockResolvedValueOnce({ id: 'run-2' });
  const second = { ...project, id: 'project-2', name: 'Second' };
  render(<NewRunForm projects={[project, second]} onCreated={vi.fn()} />);
  await screen.findByRole('option', { name: /profile-1/ });
  const user = userEvent.setup();
  await user.selectOptions(screen.getByLabelText('Subscription profile'), 'profile-1:3');
  await user.selectOptions(screen.getByLabelText('Project'), 'project-2');
  expect(screen.getByLabelText('Subscription profile')).toHaveValue('default');
  await user.type(screen.getByLabelText('Task title'), 'Second project task');
  await user.type(screen.getByLabelText('Task description'), 'Second project body');
  await user.click(screen.getByRole('button', { name: 'Create run' }));
  await waitFor(() => expect(vi.mocked(mutate)).toHaveBeenCalledTimes(2));
  expect(vi.mocked(mutate).mock.calls[0][1]).toMatchObject({ project_id: 'project-2' });
  expect(vi.mocked(mutate).mock.calls[1][1]).toEqual({ task_id: 'task-2' });
});

test('submitting while profile options are loading captures project default for uncertain retry', async () => {
  vi.mocked(api).mockImplementationOnce(() => new Promise(() => {}));
  vi.mocked(mutate).mockReset().mockResolvedValueOnce({ id: 'task-1' })
    .mockRejectedValueOnce(new Error('timeout')).mockResolvedValueOnce({ id: 'run-1' });
  render(<NewRunForm projects={[project]} onCreated={vi.fn()} />);
  const user = await fill();
  expect(screen.getByLabelText('Subscription profile')).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Create run' }));
  await screen.findByRole('alert');
  await user.click(screen.getByRole('button', { name: 'Retry creation' }));
  await waitFor(() => expect(vi.mocked(mutate)).toHaveBeenCalledTimes(3));
  expect(vi.mocked(mutate).mock.calls[1][1]).toEqual({ task_id: 'task-1' });
  expect(vi.mocked(mutate).mock.calls[2]).toEqual(vi.mocked(mutate).mock.calls[1]);
});

test('server capability exposes mutually exclusive issue import with no browser repository override', async () => {
  vi.mocked(mutate).mockReset().mockResolvedValueOnce({ id: 'imported-task' }).mockResolvedValueOnce({ id: 'imported-run' });
  const onCreated = vi.fn();
  render(<NewRunForm projects={[{ ...project, issue_import_available: true }]} onCreated={onCreated} />);
  await userEvent.selectOptions(screen.getByLabelText('Task source'), 'github');
  expect(screen.queryByLabelText('Task title')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Task description')).not.toBeInTheDocument();
  await userEvent.type(screen.getByLabelText('GitHub issue number'), '42');
  await userEvent.click(screen.getByRole('button', { name: 'Create run' }));
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith('imported-run'));
  expect(vi.mocked(mutate).mock.calls[0].slice(0, 2)).toEqual(['/tasks/import-github', { project_id: 'project-1', issue_number: 42 }]);
  expect(vi.mocked(mutate).mock.calls[1].slice(0, 2)).toEqual(['/runs', { task_id: 'imported-task' }]);
});

test('renders provider-tinted compact role cards with explicit provider labels and grouped metadata', () => {
  const multiProviderProject: components['schemas']['ProjectResponse'] = {
    ...project,
    policy: {
      runner_mode: 'docker',
      commands: [{ name: 'check', required: true }],
      planner_model: { provider: 'google', model: 'gemini-3.8-flash', max_input_tokens: 100000, max_output_tokens: 8000, max_tool_calls: 30, max_duration_seconds: 300, max_cost_minor: 150 },
      developer_model: { provider: 'openai', model: 'gpt-6-sol', max_input_tokens: 120000, max_output_tokens: 16000, max_tool_calls: 40, max_duration_seconds: 600, max_cost_minor: 300 },
      reviewer_model: { provider: 'anthropic', model: 'claude-opus-5-5', max_input_tokens: 200000, max_output_tokens: 32000, max_tool_calls: 20, max_duration_seconds: 400, max_cost_minor: 500 },
    },
  };
  render(<NewRunForm projects={[multiProviderProject]} onCreated={vi.fn()} />);

  // Role sections
  expect(screen.getByRole('region', { name: 'planner policy' })).toBeInTheDocument();
  expect(screen.getByRole('region', { name: 'developer policy' })).toBeInTheDocument();
  expect(screen.getByRole('region', { name: 'reviewer policy' })).toBeInTheDocument();

  // Explicit provider badges
  expect(screen.getByText('Google / Gemini')).toBeInTheDocument();
  expect(screen.getByText('OpenAI')).toBeInTheDocument();
  expect(screen.getByText('Anthropic / Claude')).toBeInTheDocument();

  // Models prominent
  expect(screen.getAllByText('gemini-3.8-flash').length).toBeGreaterThanOrEqual(1);
  expect(screen.getAllByText('gpt-6-sol').length).toBeGreaterThanOrEqual(1);
  expect(screen.getAllByText('claude-opus-5-5').length).toBeGreaterThanOrEqual(1);

  // Grouped budget details exist rather than single dense sentence
  expect(screen.queryByText(/Input 100000 tokens · Output 8000 tokens · 30 tools · 300 seconds · Cost limit 150 minor currency units/)).not.toBeInTheDocument();
  expect(screen.getAllByText(/All policy limits/i)).toHaveLength(3);
});
