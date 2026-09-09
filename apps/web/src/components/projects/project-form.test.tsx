import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { ProjectForm } from './project-form';
import { ApiError } from '@/lib/api/client';

afterEach(cleanup);
async function identity() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText('Project name'), 'Parallel');
  await user.type(screen.getByLabelText('Repository path'), 'C:/code/parallel');
  await user.type(screen.getByLabelText('GitHub repository'), 'owner/repo');
  return user;
}

test('submits named argv and defaults database off with separate path lists', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProjectForm onSave={save} />);
  const user = await identity();
  expect(screen.getByLabelText('Local remediation limit')).toHaveValue(3);
  expect(screen.getByLabelText('Remote remediation limit')).toHaveValue(3);
  await user.click(screen.getByRole('button', { name: 'Add command' }));
  await user.type(screen.getByLabelText('Command name'), 'test');
  await user.type(screen.getByLabelText('Executable'), 'npm');
  await user.click(screen.getByRole('button', { name: 'Add argument' }));
  await user.type(screen.getByLabelText('Argument 1'), 'test');
  await user.type(screen.getByLabelText('Allowed environment files'), '.env.example');
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({
    commands: [expect.objectContaining({ kind: 'test', name: 'test', argv: ['npm', 'test'] })],
    database: { enabled: false }, allowed_environment_files: ['.env.example'], secret_paths: ['.env', '.env.local'],
  }));
});

test('trusted host requires explicit warning acknowledgement and merge methods are constrained', async () => {
  render(<ProjectForm onSave={vi.fn()} />);
  const user = await identity();
  expect(screen.getByRole('option', { name: 'Trusted host · unsandboxed' })).toBeDisabled();
  await user.click(screen.getByLabelText(/I trust this project/));
  await user.selectOptions(screen.getByLabelText('Runner'), 'trusted_host');
  expect(screen.getByLabelText('Runner')).toHaveValue('trusted_host');
  await user.click(screen.getByLabelText(/I trust this project/));
  expect(screen.getByLabelText('Runner')).toHaveValue('docker');
  expect(screen.getByRole('group', { name: 'Allowed merge methods' }).querySelectorAll('input')).toHaveLength(3);
});

test('enabled database requires reference and key and disabling drops the reference', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProjectForm onSave={save} />);
  const user = await identity();
  await user.click(screen.getByLabelText('Provision a PostgreSQL database per worktree'));
  expect(screen.getByLabelText('Administrator secret reference')).toBeRequired();
  expect(screen.getByLabelText('Injected environment key')).toBeRequired();
  await user.type(screen.getByLabelText('Administrator secret reference'), 'secret://postgres/admin');
  await user.click(screen.getByLabelText('Provision a PostgreSQL database per worktree'));
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ database: { enabled: false } }));
});

test('server field errors preserve input without exposing exception details', async () => {
  render(<ProjectForm onSave={vi.fn().mockRejectedValue(new ApiError(422, 'private detail', { repository_path: 'Invalid value.' }))} />);
  const user = await identity();
  await user.click(screen.getByRole('button', { name: 'Register project' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('Repository path')).toHaveValue('C:/code/parallel');
  expect(screen.getByLabelText('Repository path')).toHaveAttribute('aria-invalid', 'true');
  expect(screen.queryByText('private detail')).not.toBeInTheDocument();
});
