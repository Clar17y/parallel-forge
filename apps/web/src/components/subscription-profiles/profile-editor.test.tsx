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

test('regression: uses updated offline fallback seeds and provides refresh model choices button', () => {
  const defaults = defaultRolePreferences();
  const complex = defaults.find(p => p.purpose === 'complex_implementation');
  const review = defaults.find(p => p.purpose === 'independent_review');
  const routine = defaults.find(p => p.purpose === 'routine_implementation');

  expect(complex?.preferred_route.model).toBe('gpt-6-sol');
  expect(complex?.preferred_route.effort).toBe('medium');
  expect(review?.preferred_route.model).toBe('claude-opus-5-5');
  expect(routine?.fallback_routes[0]?.model).toBe('gpt-6-luna');

  render(<ProfileEditor onSave={vi.fn()} />);
  expect(screen.getByRole('button', { name: 'Refresh model choices' })).toBeInTheDocument();
});

test('new and legacy profiles omit jev default until explicitly configured', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProfileEditor onSave={save} />);
  expect(screen.getByLabelText('Configure Jev default')).not.toBeChecked();
  await userEvent.type(screen.getByLabelText('Preferred route provider'), 'openai');
  await userEvent.type(screen.getByLabelText('Preferred route client'), 'codex');
  await userEvent.type(screen.getByLabelText('Preferred route model'), 'sol');
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(save).toHaveBeenCalledWith(expect.not.objectContaining({ jev: expect.anything() }));
  expect(save.mock.calls[0][0]).not.toHaveProperty('jev');

  cleanup();
  const legacyInitial = {
    profile_id: 'legacy-profile',
    version: 1,
    default_billing_mode: 'allowance_only' as const,
    approved_mappings: [],
    preferences: [{
      purpose: 'primary' as const,
      preferred_route: { provider: 'openai', client: 'codex', model: 'sol', effort: 'low' as const, auth_mode: 'subscription' as const, billing_mode: 'allowance_only' as const },
      fallback_routes: [],
    }],
  };
  const appendSave = vi.fn().mockResolvedValue(undefined);
  render(<ProfileEditor expectedVersion={1} initial={legacyInitial} onSave={appendSave} />);
  expect(screen.getByLabelText('Configure Jev default')).not.toBeChecked();
  await userEvent.click(screen.getByRole('button', { name: 'Append version 2' }));
  expect(appendSave.mock.calls[0][0]).not.toHaveProperty('jev');
});

test('configures and saves optional Jev default on a subscription profile', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProfileEditor onSave={save} />);
  await userEvent.type(screen.getByLabelText('Preferred route provider'), 'openai');
  await userEvent.type(screen.getByLabelText('Preferred route client'), 'codex');
  await userEvent.type(screen.getByLabelText('Preferred route model'), 'sol');

  const checkbox = screen.getByLabelText('Configure Jev default');
  expect(checkbox).not.toBeChecked();
  await userEvent.click(checkbox);
  expect(checkbox).toBeChecked();

  expect(screen.getByLabelText('Jev mode')).toHaveValue('off');
  await userEvent.selectOptions(screen.getByLabelText('Jev mode'), 'shadow');
  await userEvent.click(screen.getByLabelText('Allow remote processing of bounded, redacted source excerpts'));
  await userEvent.clear(screen.getByLabelText('Jev model alias'));
  await userEvent.type(screen.getByLabelText('Jev model alias'), 'custom-jev-alias');
  await userEvent.clear(screen.getByLabelText('Top results'));
  await userEvent.type(screen.getByLabelText('Top results'), '25');

  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({
    jev: expect.objectContaining({
      mode: 'shadow',
      allow_remote: true,
      model: 'custom-jev-alias',
      top_k: 25,
      semantic_search: true,
      review_focus: true,
    }),
  }));
});

test('loaded custom Jev limits and settings survive unrelated route edits and failed save', async () => {
  let saveCount = 0;
  const save = vi.fn().mockImplementation(async () => {
    saveCount++;
    if (saveCount === 1) throw new Error('Temporary API error');
    return undefined;
  });

  const initial = {
    profile_id: 'profile-jev-1',
    version: 2,
    default_billing_mode: 'allowance_only' as const,
    approved_mappings: [],
    preferences: [{
      purpose: 'primary' as const,
      preferred_route: { provider: 'openai', client: 'codex', model: 'sol', effort: 'low' as const, auth_mode: 'subscription' as const, billing_mode: 'allowance_only' as const },
      fallback_routes: [],
    }],
    jev: {
      mode: 'on' as const,
      allow_remote: true,
      model: 'pinned-jev-v2',
      semantic_search: true,
      review_focus: false,
      top_k: 42,
      max_requests_per_run: 80,
      max_input_units_per_run: 500000,
      max_candidates: 50,
      max_result_chars: 15000,
      timeout_seconds: 2.5,
      cache_ttl_seconds: 7200,
    },
  };

  render(<ProfileEditor expectedVersion={2} initial={initial} onSave={save} />);

  expect(screen.getByLabelText('Configure Jev default')).toBeChecked();
  expect(screen.getByLabelText('Jev mode')).toHaveValue('on');
  expect(screen.getByLabelText('Allow remote processing of bounded, redacted source excerpts')).toBeChecked();
  expect(screen.getByLabelText('Jev model alias')).toHaveValue('pinned-jev-v2');
  expect(screen.getByLabelText('Top results')).toHaveValue(42);
  expect(screen.getByLabelText('Maximum requests per run')).toHaveValue(80);
  expect(screen.getByLabelText('Request timeout seconds')).toHaveValue(2.5);
  expect(screen.getByLabelText('Request timeout seconds')).toBeValid();

  // Unrelated route edit: edit preferred route client
  await userEvent.clear(screen.getByLabelText('Preferred route client'));
  await userEvent.type(screen.getByLabelText('Preferred route client'), 'codex_app_server');

  // Trigger catalog refresh
  await userEvent.click(screen.getByRole('button', { name: 'Refresh model choices' }));

  // First save fails
  await userEvent.click(screen.getByRole('button', { name: 'Append version 3' }));
  expect(await screen.findByText('Temporary API error')).toBeInTheDocument();

  // Form retains all Jev settings and the route edit
  expect(screen.getByLabelText('Configure Jev default')).toBeChecked();
  expect(screen.getByLabelText('Jev mode')).toHaveValue('on');
  expect(screen.getByLabelText('Allow remote processing of bounded, redacted source excerpts')).toBeChecked();
  expect(screen.getByLabelText('Jev model alias')).toHaveValue('pinned-jev-v2');
  expect(screen.getByLabelText('Top results')).toHaveValue(42);
  expect(screen.getByLabelText('Preferred route client')).toHaveValue('codex_app_server');

  // Retry save succeeds and sends preserved Jev config
  await userEvent.click(screen.getByRole('button', { name: 'Append version 3' }));
  expect(save).toHaveBeenCalledTimes(2);
  expect(save.mock.calls[1][0]).toEqual(expect.objectContaining({
    expected_current_version: 2,
    jev: expect.objectContaining({
      mode: 'on',
      allow_remote: true,
      model: 'pinned-jev-v2',
      top_k: 42,
      max_requests_per_run: 80,
      max_input_units_per_run: 500000,
      max_candidates: 50,
      max_result_chars: 15000,
      timeout_seconds: 2.5,
      cache_ttl_seconds: 7200,
      semantic_search: true,
      review_focus: false,
    }),
  }));
});

test('allows explicit removal of Jev default when appending profile version', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const initial = {
    profile_id: 'profile-jev-removable',
    version: 1,
    default_billing_mode: 'allowance_only' as const,
    approved_mappings: [],
    preferences: [{
      purpose: 'primary' as const,
      preferred_route: { provider: 'openai', client: 'codex', model: 'sol', effort: 'low' as const, auth_mode: 'subscription' as const, billing_mode: 'allowance_only' as const },
      fallback_routes: [],
    }],
    jev: {
      mode: 'shadow' as const,
      allow_remote: false,
      model: 'jev-latest',
      semantic_search: true,
      review_focus: true,
      top_k: 15,
      max_requests_per_run: 64,
      max_input_units_per_run: 250000,
      max_candidates: 96,
      max_result_chars: 12000,
      timeout_seconds: 15,
      cache_ttl_seconds: 3600,
    },
  };

  render(<ProfileEditor expectedVersion={1} initial={initial} onSave={save} />);
  const checkbox = screen.getByLabelText('Configure Jev default');
  expect(checkbox).toBeChecked();

  // Explicitly remove Jev default
  await userEvent.click(checkbox);
  expect(checkbox).not.toBeChecked();

  await userEvent.click(screen.getByRole('button', { name: 'Append version 2' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({
    expected_current_version: 1,
    jev: null,
  }));
});
