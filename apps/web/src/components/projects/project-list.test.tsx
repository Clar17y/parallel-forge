import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';
import { ProjectList } from './project-list';
import type { components } from '@/lib/api/schema';

afterEach(cleanup);

const project: components['schemas']['ProjectResponse'] = {
  id: 'project-1', name: 'Parallel', repository_path: 'C:/code/parallel',
  canonical_path_key: 'c:/code/parallel', github_repository: 'owner/repo', default_branch: 'main',
  instructions_path: null, policy_version: 1, policy_digest: 'a'.repeat(64),
  issue_import_available: false,
  policy: { runner_mode: 'docker', commands: [], planner_model: { provider: 'google', model: 'test-model' } },
};

test('empty project list explains the next step and links to registration', () => {
  render(<ProjectList projects={[]} />);
  expect(screen.getByText('No projects registered yet.')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Register your first project' })).toHaveAttribute('href', '/projects/new');
});

test('lists projects with links to their pages', () => {
  render(<ProjectList projects={[project]} />);
  expect(screen.getByRole('link', { name: 'Parallel' })).toHaveAttribute('href', '/projects/project-1');
  expect(screen.queryByText('No projects registered yet.')).not.toBeInTheDocument();
});
