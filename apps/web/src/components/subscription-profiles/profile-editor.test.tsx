import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { ProfileEditor, defaultRolePreferences } from './profile-editor';

afterEach(cleanup);
test('creates a structured request with a primary route and explicit billing', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProfileEditor onSave={save} />);
  await userEvent.type(screen.getByLabelText('Preferred route provider'), 'openai');
  await userEvent.type(screen.getByLabelText('Preferred route client'), 'codex_app_server');
  await userEvent.type(screen.getByLabelText('Preferred route model'), 'sol');
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ default_billing_mode: 'allowance_only', preferences: [{ purpose: 'primary', preferred_route: expect.objectContaining({ provider: 'openai', client: 'codex_app_server', model: 'sol' }), fallback_routes: [] }] }));
  expect(save.mock.calls[0][0]).not.toHaveProperty('expected_current_version');
});

test('preserves mappings and complete fallback route fields when appending', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const initial = { profile_id: 'profile-1', version: 3, default_billing_mode: 'allowance_only', approved_mappings: [{ requested_model: 'requested', effective_model: 'effective', reason: 'approved' }], preferences: [{ purpose: 'primary', preferred_route: { provider: 'openai', client: 'codex_app_server', model: 'sol', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' }, fallback_routes: [{ provider: 'anthropic', client: 'claude', model: 'opus', effort: 'high', auth_mode: 'subscription', billing_mode: 'allowance_only' }] }] };
  render(<ProfileEditor expectedVersion={3} initial={initial} onSave={save} />);
  expect(screen.getByLabelText('Requested mapping 1')).toHaveValue('requested');
  expect(screen.getByLabelText('Fallback route 1 effort')).toHaveValue('high');
  await userEvent.click(screen.getByRole('button', { name: 'Append version 4' }));
  expect(save.mock.calls[0][0]).toEqual(expect.objectContaining({ approved_mappings: initial.approved_mappings, preferences: [expect.objectContaining({ fallback_routes: [expect.objectContaining({ effort: 'high', auth_mode: 'subscription', billing_mode: 'allowance_only' })] })] }));
});

test('appends with the current version and preserves immutable version source', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProfileEditor expectedVersion={3} initial={{ profile_id: 'profile-1', version: 3, default_billing_mode: 'allowance_only', approved_mappings: [], preferences: [{ purpose: 'primary', preferred_route: { provider: 'openai', client: 'codex', model: 'sol', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' }, fallback_routes: [] }] }} onSave={save} />);
  await userEvent.click(screen.getByRole('button', { name: 'Append version 4' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ expected_current_version: 3 }));
});

test('offers the agreed editable defaults only for a new profile', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProfileEditor onSave={save} />);
  await userEvent.click(screen.getByRole('button', { name: 'Use default role preferences' }));
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  const request = save.mock.calls[0][0];
  expect(request.preferences).toEqual(defaultRolePreferences());
  expect(request.preferences.find((item: { purpose: string }) => item.purpose === 'primary').fallback_routes).toEqual([]);
  cleanup();
  render(<ProfileEditor expectedVersion={1} initial={{ profile_id: 'profile-1', version: 1, default_billing_mode: 'allowance_only', approved_mappings: [], preferences: [defaultRolePreferences()[0]] }} onSave={save} />);
  expect(screen.queryByRole('button', { name: 'Use default role preferences' })).not.toBeInTheDocument();
});

test('uses the approved official Gemini model and separate effort for routine defaults', () => {
  const routine = defaultRolePreferences().find(
    preference => preference.purpose === 'routine_implementation',
  );

  expect(routine?.preferred_route).toEqual({
    provider: 'google',
    client: 'gemini_cli',
    model: 'gemini-3.8-flash',
    effort: 'medium',
    auth_mode: 'subscription',
    billing_mode: 'allowance_only',
  });
});
