import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { NewRunForm } from './new-run-form';
import { mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';

vi.mock('@/lib/api/client', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/api/client')>(), mutate: vi.fn(),
}));
afterEach(cleanup);
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
  await user.click(screen.getByRole('button', { name: 'Retry creation' }));
  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(3));
  expect(vi.mocked(mutate).mock.calls[2]).toEqual(vi.mocked(mutate).mock.calls[1]);
  expect(screen.getByLabelText('Task title')).toHaveValue('Build feature');
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
