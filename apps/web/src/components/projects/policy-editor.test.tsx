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
  expect(payload).not.toHaveProperty('jev');
  expect(document.local_remediation_limit).toBe(5);
});

test('round-trips an explicitly configured Jev policy into a new immutable version', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const jev = { mode: 'shadow' as const, allow_remote: true, model: 'operator-alias', semantic_search: true,
    review_focus: false, top_k: 8, max_requests_per_run: 20, max_input_units_per_run: 10000,
    max_candidates: 30, max_result_chars: 5000, timeout_seconds: 8, cache_ttl_seconds: 100 };
  const document = { ...projectDefaults, jev };
  render(<PolicyEditor policy={{ project_id: 'project-1', version: 2, policy_version: 2,
    policy_digest: 'c'.repeat(64), document_schema_version: 1, document }} onSave={save} />);
  expect(screen.getByLabelText('Configure Jev for this project')).toBeChecked();
  expect(screen.getByLabelText('Jev model alias')).toHaveValue('operator-alias');
  await userEvent.click(screen.getByRole('button', { name: 'Create policy version' }));
  expect(save.mock.calls[0][0]).toMatchObject({ expected_policy_version: 2, jev });
});

test('keeps an explicit Jev Off override separate from returning to profile inheritance', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const jev = { mode: 'on' as const, allow_remote: true, model: 'operator-alias', semantic_search: true,
    review_focus: true, top_k: 8, max_requests_per_run: 20, max_input_units_per_run: 10000,
    max_candidates: 30, max_result_chars: 5000, timeout_seconds: 8, cache_ttl_seconds: 100 };
  render(<PolicyEditor policy={{ project_id: 'project-1', version: 2, policy_version: 2,
    policy_digest: 'c'.repeat(64), document_schema_version: 1, document: { ...projectDefaults, jev } }} onSave={save} />);
  expect(screen.getByLabelText('Configure Jev for this project')).toBeEnabled();
  await userEvent.selectOptions(screen.getByRole('combobox', { name: /Jev mode/ }), 'off');
  await userEvent.click(screen.getByRole('button', { name: 'Create policy version' }));
  expect(save.mock.calls[0][0]).toMatchObject({ expected_policy_version: 2, jev: { ...jev, mode: 'off' } });
  await userEvent.click(screen.getByLabelText('Configure Jev for this project'));
  await userEvent.click(screen.getByRole('button', { name: 'Create policy version' }));
  expect(save.mock.calls[1][0]).not.toHaveProperty('jev');
});
