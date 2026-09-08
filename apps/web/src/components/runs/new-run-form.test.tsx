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
