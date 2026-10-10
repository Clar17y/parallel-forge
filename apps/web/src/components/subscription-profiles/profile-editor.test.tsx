import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { ProfileEditor, defaultRolePreferences } from './profile-editor';

afterEach(cleanup);
function presetValue(selectLabel: string, optionLabel: string) {
  const select = screen.getByLabelText(selectLabel) as HTMLSelectElement;
  return Array.from(select.options).find(option => option.textContent?.includes(optionLabel))!.value;
}
test('creates a structured request with a primary route and explicit billing', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProfileEditor onSave={save} />);
  await userEvent.selectOptions(screen.getByLabelText('Primary model'), presetValue('Primary model', 'Sol 6.1'));
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ default_billing_mode: 'allowance_only', preferences: expect.arrayContaining([expect.objectContaining({ purpose: 'primary', preferred_route: expect.objectContaining({ provider: 'openai', client: 'codex_app_server', model: 'gpt-6.1-sol', effort: 'maximum', billing_mode: 'allowance_only' }), fallback_routes: [] })]) }));
  expect(save.mock.calls[0][0]).not.toHaveProperty('expected_current_version');
});

test('starts with useful role presets and atomically saves a complete model choice', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProfileEditor onSave={save} />);
  expect(screen.getByLabelText('Primary model')).toBeInTheDocument();
  expect(screen.getByLabelText('Implementer model')).toBeInTheDocument();
  expect(screen.getByLabelText('Reviewer model')).toBeInTheDocument();
  const geminiOption = presetValue('Primary model', 'Google Gemini 3.8 Flash');
  await userEvent.selectOptions(screen.getByLabelText('Primary model'), geminiOption);
  await userEvent.selectOptions(screen.getByLabelText('Primary reasoning'), 'medium');
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(save.mock.calls[0][0].preferences[0].preferred_route).toEqual({
    provider: 'google', client: 'gemini_cli', model: 'gemini-3.8-flash', effort: 'medium',
    auth_mode: 'subscription', billing_mode: 'allowance_only',
  });
});

test('shows compact role rows with one model chooser and one collapsed advanced panel each', () => {
  render(<ProfileEditor onSave={vi.fn()} />);
  const rows = document.querySelectorAll('section[class*="row"]');
  expect(rows).toHaveLength(9);
  for (const row of rows) {
    expect(row.querySelector(':scope > label select[aria-label$=" model"]')).toBeInTheDocument();
    expect(row.querySelectorAll('details')).toHaveLength(1);
  }
  const primary = screen.getByLabelText('Primary model') as HTMLSelectElement;
  const labels = Array.from(primary.options).map(option => option.textContent ?? '');
  expect(labels).toContain('Sol 6.1');
  expect(labels).toContain('Google Gemini 3.8 Flash');
  expect(labels).toContain('Claude Opus 5.5');
  expect(labels.some(label => /codex_app_server|gemini_cli|offline suggestion/i.test(label))).toBe(false);
  expect(screen.getByLabelText('Primary reasoning')).toBeInTheDocument();
});

test('renders visible reasoning labels in RouteFields without changing aria-label names or effort persistence', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProfileEditor onSave={save} />);
  const reasoningSelect = screen.getByLabelText('Primary reasoning');
  expect(reasoningSelect).toBeInTheDocument();
  const reasoningLabels = screen.getAllByText('Reasoning');
  expect(reasoningLabels.length).toBeGreaterThan(0);
  expect(reasoningLabels[0]).not.toHaveClass('sr-only');
  await userEvent.selectOptions(reasoningSelect, '__custom__');
  await userEvent.selectOptions(reasoningSelect, 'high');
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(save.mock.calls[0][0].preferences[0].preferred_route.effort).toBe('high');
});

test('edits reasoning independently from the model and saves Astra 6 Low', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProfileEditor onSave={save} />);
  const model = screen.getByLabelText('Primary model') as HTMLSelectElement;
  expect(Array.from(model.options).map(option => option.textContent)).toContain('Astra 6');
  await userEvent.selectOptions(screen.getByLabelText('Primary reasoning'), 'low');
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(save.mock.calls[0][0].preferences[0].preferred_route).toEqual(expect.objectContaining({ model: 'gpt-6-astra', effort: 'low' }));
});

test('keeps catalog levels authoritative and offers enum overrides only on request', async () => {
  const catalogPage = { catalogs: [{ provider: 'openai', client: 'codex_app_server', source: 'provider', status: 'available', observed_at: '2026-10-01T00:00:00Z', stale: false, models: [{ id: 'gpt-6-astra', label: 'Astra', efforts: ['low'] }], message: '' }], next_cursor: null };
  render(<ProfileEditor catalogPage={catalogPage as never} onSave={vi.fn()} />);
  const reasoning = screen.getByLabelText('Primary reasoning') as HTMLSelectElement;
  expect(Array.from(reasoning.options).map(option => option.value)).toEqual(['low', '__custom__']);
  await userEvent.selectOptions(reasoning, '__custom__');
  expect(screen.getByLabelText('Primary reasoning')).toHaveValue('low');
  await userEvent.selectOptions(screen.getByLabelText('Primary reasoning'), 'high');
  expect(screen.getByLabelText('Primary reasoning')).toHaveValue('high');
});

test('reasoning-only edits preserve route identity, auth, billing, and fallbacks', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const preferred = { provider: 'custom-provider', client: 'local-client', model: 'private-model', effort: 'high', auth_mode: 'api_key', billing_mode: 'paid_opt_in' };
  const fallback = { provider: 'google', client: 'gemini_cli', model: 'gemini-3.8-flash', effort: 'medium', auth_mode: 'subscription', billing_mode: 'allowance_only' };
  const initial = { profile_id: 'profile-exact', version: 2, default_billing_mode: 'allowance_only', approved_mappings: [], preferences: [{ purpose: 'primary', preferred_route: preferred, fallback_routes: [fallback] }] };
  render(<ProfileEditor initial={initial as never} expectedVersion={2} onSave={save} />);
  await userEvent.selectOptions(screen.getByLabelText('Primary reasoning'), '__custom__');
  await userEvent.selectOptions(screen.getByLabelText('Primary reasoning'), 'medium');
  await userEvent.click(screen.getByRole('button', { name: 'Append version 3' }));
  expect(save.mock.calls[0][0].preferences[0].preferred_route).toEqual({ ...preferred, effort: 'medium' });
  expect(save.mock.calls[0][0].preferences[0].fallback_routes).toEqual([fallback]);
});

test('does not offer Add role when all nine purposes are already present', () => {
  render(<ProfileEditor onSave={vi.fn()} />);
  expect(screen.queryByRole('button', { name: 'Add role preference' })).not.toBeInTheDocument();
});

test('repeated additions from a legacy subset use unused complete role defaults and preserve existing route edits', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const defaults = defaultRolePreferences();
  render(<ProfileEditor initial={{ profile_id: 'subset', version: 1, default_billing_mode: 'allowance_only', approved_mappings: [], preferences: defaults.slice(0, 2) }} expectedVersion={1} onSave={save} />);
  await userEvent.selectOptions(screen.getByLabelText('Implementer model'), presetValue('Implementer model', 'Sol 6.1'));
  await userEvent.click(screen.getByRole('button', { name: 'Add role preference' }));
  await userEvent.click(screen.getByRole('button', { name: 'Add role preference' }));
  expect(screen.getByLabelText('Purpose 3')).toHaveValue('complex_implementation');
  expect(screen.getByLabelText('Purpose 4')).toHaveValue('independent_review');
  await userEvent.click(screen.getByRole('button', { name: 'Append version 2' }));
  expect(save.mock.calls[0][0].preferences).toEqual([
    expect.objectContaining({ purpose: 'primary', preferred_route: defaults[0].preferred_route }),
    expect.objectContaining({ purpose: 'routine_implementation', preferred_route: expect.objectContaining({ model: 'gpt-6.1-sol', effort: 'maximum' }) }),
    expect.objectContaining({ purpose: 'complex_implementation', preferred_route: defaults[2].preferred_route }),
    expect.objectContaining({ purpose: 'independent_review', preferred_route: defaults[3].preferred_route }),
  ]);
});

test('removing and re-adding Primary restores a saveable primary route without changing other models', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const defaults = defaultRolePreferences();
  const initial = { profile_id: 'all-roles', version: 5, default_billing_mode: 'allowance_only', approved_mappings: [], preferences: defaults };
  render(<ProfileEditor initial={initial} expectedVersion={5} onSave={save} />);
  await userEvent.click(screen.getAllByText('Advanced')[0]);
  await userEvent.click(screen.getAllByRole('button', { name: 'Remove role' })[0]);
  expect(screen.getByRole('button', { name: 'Add role preference' })).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Add role preference' }));
  expect(screen.queryByRole('button', { name: 'Add role preference' })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Append version 6' }));
  const saved = save.mock.calls[0][0].preferences;
  expect(saved).toHaveLength(9);
  expect(saved[8]).toEqual(expect.objectContaining({ purpose: 'primary', preferred_route: defaults[0].preferred_route }));
  expect(saved.slice(0, 8)).toEqual(defaults.slice(1));
});

test('an explicit model choice preserves saved authentication and billing', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const initial = { profile_id: 'profile-api', version: 2, default_billing_mode: 'allowance_only', approved_mappings: [], preferences: [{
    purpose: 'primary', preferred_route: { provider: 'openai', client: 'codex', model: 'gpt-6.1-sol', effort: 'high', auth_mode: 'api_key', billing_mode: 'paid_opt_in' }, fallback_routes: [],
  }] };
  render(<ProfileEditor expectedVersion={2} initial={initial} onSave={save} />);
  await userEvent.selectOptions(screen.getByLabelText('Primary model'), presetValue('Primary model', 'Google Gemini 3.8 Flash'));
  await userEvent.click(screen.getByRole('button', { name: 'Append version 3' }));
  expect(save.mock.calls[0][0].preferences[0].preferred_route).toEqual({
    provider: 'google', client: 'gemini_cli', model: 'gemini-3.8-flash', effort: 'medium',
    auth_mode: 'api_key', billing_mode: 'paid_opt_in',
  });
});

test('an unchanged custom API-key and paid route remains exact on save', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const route = { provider: 'custom-provider', client: 'local-client', model: 'private-model', effort: 'high', auth_mode: 'api_key', billing_mode: 'paid_opt_in' };
  const initial = { profile_id: 'profile-custom', version: 4, default_billing_mode: 'allowance_only', approved_mappings: [], preferences: [{ purpose: 'primary', preferred_route: route, fallback_routes: [] }] };
  render(<ProfileEditor expectedVersion={4} initial={initial} onSave={save} />);
  await userEvent.click(screen.getByRole('button', { name: 'Append version 5' }));
  expect(save.mock.calls[0][0].preferences[0].preferred_route).toEqual(route);
});

test('an unchanged catalog-backed route retains its saved unsupported reasoning level', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const route = { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'high', auth_mode: 'api_key', billing_mode: 'paid_opt_in' };
  const catalogPage = { catalogs: [{ provider: 'openai', client: 'codex_app_server', source: 'provider', status: 'available', observed_at: '2026-10-01T00:00:00Z', stale: false, models: [{ id: 'gpt-6-astra', label: 'Astra', efforts: ['low'] }], message: '' }], next_cursor: null };
  const initial = { profile_id: 'legacy-effort', version: 1, default_billing_mode: 'allowance_only', approved_mappings: [], preferences: [{ purpose: 'primary', preferred_route: route, fallback_routes: [] }] };
  render(<ProfileEditor initial={initial as never} expectedVersion={1} catalogPage={catalogPage as never} onSave={save} />);
  expect(screen.getByLabelText('Primary reasoning')).toHaveValue('high');
  await userEvent.click(screen.getByRole('button', { name: 'Append version 2' }));
  expect(save.mock.calls[0][0].preferences[0].preferred_route).toEqual(route);
});

test('preserves mappings and complete fallback route fields when appending', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const initial = { profile_id: 'profile-1', version: 3, default_billing_mode: 'allowance_only', approved_mappings: [{ requested_model: 'requested', effective_model: 'effective', reason: 'approved' }], preferences: [{ purpose: 'primary', preferred_route: { provider: 'openai', client: 'codex_app_server', model: 'sol', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' }, fallback_routes: [{ provider: 'anthropic', client: 'claude', model: 'opus', effort: 'high', auth_mode: 'api_key', billing_mode: 'paid_opt_in' }] }] };
  render(<ProfileEditor expectedVersion={3} initial={initial} onSave={save} />);
  await userEvent.click(screen.getByText('Advanced profile details'));
  await userEvent.click(screen.getByText('Advanced'));
  expect(screen.getByLabelText('Requested mapping 1')).toHaveValue('requested');
  expect(screen.getByLabelText('Primary fallback route 1 reasoning')).toHaveValue('high');
  await userEvent.click(screen.getByRole('button', { name: 'Append version 4' }));
  expect(save.mock.calls[0][0]).toEqual(expect.objectContaining({ approved_mappings: initial.approved_mappings, preferences: [expect.objectContaining({ fallback_routes: [expect.objectContaining({ effort: 'high', auth_mode: 'api_key', billing_mode: 'paid_opt_in' })] })] }));
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
  expect(screen.getByLabelText('Primary model')).toBeInTheDocument();
  expect(screen.getByLabelText('Implementer model')).toBeInTheDocument();
  expect(screen.getByLabelText('Reviewer model')).toBeInTheDocument();
  cleanup();
  render(<ProfileEditor expectedVersion={1} initial={{ profile_id: 'profile-1', version: 1, default_billing_mode: 'allowance_only', approved_mappings: [], preferences: [defaultRolePreferences()[0]] }} onSave={save} />);
  expect(screen.getAllByLabelText('Primary model')).toHaveLength(1);
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

test('role token budgets inherit by default, survive model edits, and can return to inheritance', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProfileEditor onSave={save} />);
  await userEvent.click(screen.getAllByText('Advanced')[0]);
  expect(screen.getAllByText('Input tokens: Use run/task default').length).toBeGreaterThan(0);
  await userEvent.click(screen.getByRole('button', { name: 'Set Primary input token budget' }));
  expect(screen.getByRole('slider', { name: 'Primary input token budget' })).toHaveValue('525000');
  await userEvent.selectOptions(screen.getByLabelText('Primary model'), presetValue('Primary model', 'Google Gemini 3.8 Flash'));
  expect(screen.getByRole('slider', { name: 'Primary input token budget' })).toHaveValue('525000');
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(save.mock.calls[0][0].preferences[0].token_budget).toEqual({ max_input_tokens: 525000 });

  await userEvent.click(screen.getByRole('button', { name: 'Primary input tokens use run/task default' }));
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(save.mock.calls[1][0].preferences[0].token_budget).toEqual({ max_input_tokens: null });
});

test('appending a saved above-reference budget and zero retains exact values across a smaller model choice', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  const initial = {
    profile_id: 'saved-budget', version: 2, default_billing_mode: 'allowance_only', approved_mappings: [],
    preferences: [{ ...defaultRolePreferences()[0], token_budget: { max_input_tokens: 2_000_000, max_output_tokens: 0 } }],
  };
  render(<ProfileEditor initial={initial} expectedVersion={2} onSave={save} />);
  await userEvent.click(screen.getByText('Advanced'));
  expect(screen.getByRole('slider', { name: 'Primary input token budget' })).toHaveValue('2000000');
  expect(screen.getByRole('slider', { name: 'Primary output token budget' })).toHaveValue('0');
  await userEvent.selectOptions(screen.getByLabelText('Primary model'), presetValue('Primary model', 'Google Gemini 3.8 Flash'));
  await userEvent.click(screen.getByRole('button', { name: 'Append version 3' }));
  expect(save.mock.calls[0][0].preferences[0].token_budget).toEqual({ max_input_tokens: 2_000_000, max_output_tokens: 0 });
});

test('choosing a model leaves explicit authentication and billing choices intact', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProfileEditor onSave={save} />);
  await userEvent.click(screen.getAllByText('Advanced')[0]);
  await userEvent.selectOptions(screen.getByLabelText('Primary authentication'), 'api_key');
  await userEvent.selectOptions(screen.getByLabelText('Primary billing'), 'paid_opt_in');
  await userEvent.selectOptions(screen.getByLabelText('Primary model'), presetValue('Primary model', 'Google Gemini 3.8 Flash'));
  await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
  expect(save.mock.calls[0][0].preferences[0].preferred_route).toEqual(expect.objectContaining({
    model: 'gemini-3.8-flash', auth_mode: 'api_key', billing_mode: 'paid_opt_in',
  }));
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
  await userEvent.click(screen.getByText('Advanced profile details'));
  expect(screen.getByLabelText('Configure Jev default')).not.toBeChecked();
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
  await userEvent.click(screen.getByText('Advanced profile details'));
  expect(screen.getByLabelText('Configure Jev default')).not.toBeChecked();
  await userEvent.click(screen.getByRole('button', { name: 'Append version 2' }));
  expect(appendSave.mock.calls[0][0]).not.toHaveProperty('jev');
});

test('configures and saves optional Jev default on a subscription profile', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ProfileEditor onSave={save} />);
  await userEvent.click(screen.getByText('Advanced profile details'));

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
  await userEvent.click(screen.getByText('Advanced profile details'));
  await userEvent.click(screen.getByText('Advanced'));

  expect(screen.getByLabelText('Configure Jev default')).toBeChecked();
  expect(screen.getByLabelText('Jev mode')).toHaveValue('on');
  expect(screen.getByLabelText('Allow remote processing of bounded, redacted source excerpts')).toBeChecked();
  expect(screen.getByLabelText('Jev model alias')).toHaveValue('pinned-jev-v2');
  expect(screen.getByLabelText('Top results')).toHaveValue(42);
  expect(screen.getByLabelText('Maximum requests per run')).toHaveValue(80);
  expect(screen.getByLabelText('Request timeout seconds')).toHaveValue(2.5);
  expect(screen.getByLabelText('Request timeout seconds')).toBeValid();

  // Unrelated route edit: edit preferred route client
  await userEvent.clear(screen.getByLabelText('Primary client'));
  await userEvent.type(screen.getByLabelText('Primary client'), 'codex_app_server');

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
  expect(screen.getByLabelText('Primary client')).toHaveValue('codex_app_server');

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
  await userEvent.click(screen.getByText('Advanced profile details'));
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
