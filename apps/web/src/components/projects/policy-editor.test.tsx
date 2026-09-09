import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { PolicyEditor } from './policy-editor';
import { projectDefaults } from './project-form';

afterEach(cleanup);
test('creates a new version with exact expected version and no mutable project identity', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const document = { ...projectDefaults, local_remediation_limit: 5, id: 'project-1', base_sha: 'a'.repeat(40) };
  render(<PolicyEditor policy={{ project_id: 'project-1', version: 7, policy_version: 7,
    policy_digest: 'b'.repeat(64), document_schema_version: 1, document }} onSave={save} />);
  expect(screen.getByLabelText('Local remediation limit')).toHaveValue(5);
  expect(screen.queryByLabelText('Repository path')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Create policy version' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ expected_policy_version: 7, local_remediation_limit: 5 }));
  const payload = save.mock.calls[0][0];
  expect(payload).not.toHaveProperty('id');
  expect(payload).not.toHaveProperty('repository_path');
  expect(payload).not.toHaveProperty('base_sha');
  expect(document.local_remediation_limit).toBe(5);
});
