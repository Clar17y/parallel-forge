import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, test, vi } from 'vitest';
import { ProfileEditor } from './profile-editor';
import { defaultRolePreferences, extractNumericVersion, compareNumericVersions, deriveModelFromCatalogs } from './models';
import { routePresetIdentity } from '@/components/model-selection/model-presets';
import { SubscriptionRuntimeStatus } from './runtime-status';
import type { SubscriptionModelCatalogPage, SubscriptionModelCatalogView } from './models';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const sampleCatalogs: SubscriptionModelCatalogView[] = [
  {
    provider: 'openai',
    client: 'codex_app_server',
    source: 'provider',
    status: 'available',
    observed_at: '2026-09-29T18:00:00Z',
    stale: false,
    models: [
      { id: 'gpt-6-astra', label: 'GPT-6 Astra', efforts: ['low', 'medium'] },
      { id: 'gpt-6-sol', label: 'GPT-6 Sol', efforts: ['low', 'medium', 'high'] },
      { id: 'gpt-7-sol', label: 'GPT-7 Sol', efforts: ['low', 'medium', 'high'] },
      { id: 'gpt-10-sol', label: 'GPT-10 Sol', efforts: ['low'] }, // low effort only, not compatible with medium/high
      { id: 'gpt-6-luna', label: 'GPT-6 Luna', efforts: ['medium'] },
      { id: 'gpt-7-luna', label: 'GPT-7 Luna', efforts: ['medium'] },
    ],
    message: 'Current provider catalog choices.',
  },
  {
    provider: 'google',
    client: 'gemini_cli',
    source: 'configured',
    status: 'available',
    observed_at: '2026-09-29T17:30:00Z',
    stale: true,
    models: [
      { id: 'gemini-3.8-flash', label: 'Gemini 3.8 Flash', efforts: ['low', 'medium'] },
      { id: 'gemini-4-flash', label: 'Gemini 4 Flash', efforts: ['medium'] },
    ],
    message: 'Stale configuration cache.',
  },
  {
    provider: 'anthropic',
    client: 'claude_code',
    source: 'provider',
    status: 'available',
    observed_at: '2026-09-29T18:10:00Z',
    stale: false,
    models: [
      { id: 'claude-opus-5-5', label: 'Claude Opus 5.5', efforts: ['medium'] },
      { id: 'claude-opus-6', label: 'Claude Opus 6', efforts: ['medium'] },
    ],
    message: 'Provider reported.',
  },
];

const catalogPage: SubscriptionModelCatalogPage = {
  observed_at: '2026-09-29T18:15:00Z',
  catalogs: sampleCatalogs,
};

describe('numeric versioning and catalog derivation', () => {
  test('extracts numeric versions correctly', () => {
    expect(extractNumericVersion('gpt-6-astra')).toEqual([6]);
    expect(extractNumericVersion('gpt-5.6-luna')).toEqual([5, 6]);
    expect(extractNumericVersion('claude-opus-5-5')).toEqual([5, 5]);
    expect(extractNumericVersion('gemini-3.8-flash')).toEqual([3, 8]);
    expect(extractNumericVersion('gpt-10-sol')).toEqual([10]);
  });

  test('compares numeric versions correctly without lexical bugs', () => {
    // In lexical comparison, 'gpt-10' < 'gpt-7', which is a bug.
    // In numeric comparison, [10] > [7].
    expect(compareNumericVersions([10], [7])).toBeGreaterThan(0);
    expect(compareNumericVersions([7], [6])).toBeGreaterThan(0);
    expect(compareNumericVersions([5, 6], [5, 5])).toBeGreaterThan(0);
    expect(compareNumericVersions([6], [5, 6])).toBeGreaterThan(0);
    expect(compareNumericVersions([6], [6])).toBe(0);
  });

  test('derives newer family models while respecting effort compatibility', () => {
    // complex_implementation needs 'medium' effort and 'sol' family.
    // gpt-10-sol has highest version but only supports 'low'.
    // gpt-7-sol supports 'medium'.
    // Result must be gpt-7-sol, not gpt-10-sol.
    const complexDerived = deriveModelFromCatalogs(
      {
        provider: 'openai',
        client: 'codex_app_server',
        family: 'sol',
        effort: 'medium',
        seedModel: 'gpt-6-sol',
      },
      sampleCatalogs
    );
    expect(complexDerived.model).toBe('gpt-7-sol');
    expect(complexDerived.source).toBe('catalog');

    // routine fallback needs 'luna' family and 'medium' effort.
    // gpt-7-luna is newer than gpt-6-luna and supports medium.
    const lunaDerived = deriveModelFromCatalogs(
      {
        provider: 'openai',
        client: 'codex_app_server',
        family: 'luna',
        effort: 'medium',
        seedModel: 'gpt-6-luna',
      },
      sampleCatalogs
    );
    expect(lunaDerived.model).toBe('gpt-7-luna');
    expect(lunaDerived.source).toBe('catalog');
  });

  test('defaultRolePreferences derives catalog models when provided and labels fallback seeds', () => {
    const preferencesWithCatalog = defaultRolePreferences(sampleCatalogs);
    const complex = preferencesWithCatalog.find(p => p.purpose === 'complex_implementation');
    const routine = preferencesWithCatalog.find(p => p.purpose === 'routine_implementation');
    const review = preferencesWithCatalog.find(p => p.purpose === 'independent_review');

    expect(complex?.preferred_route.model).toBe('gpt-7-sol');
    expect(routine?.preferred_route.model).toBe('gemini-3.8-flash');
    expect(routine?.fallback_routes[0]?.model).toBe('gpt-7-luna');
    expect(review?.preferred_route.model).toBe('claude-opus-6');

    // Offline / seed fallback when catalogs unavailable
    const offlinePreferences = defaultRolePreferences();
    const offlineComplex = offlinePreferences.find(p => p.purpose === 'complex_implementation');
    expect(offlineComplex?.preferred_route.model).toBe('gpt-6-sol');
  });

  test('uses a compatible current model even when the offline suggestion is newer', () => {
    const catalogs: SubscriptionModelCatalogView[] = [{
      ...sampleCatalogs[0],
      models: [
        { id: 'gpt-5.5-sol', label: 'GPT-5.5 Sol', efforts: ['medium'] },
        { id: 'gpt-5.6-sol', label: 'GPT-5.6 Sol', efforts: ['medium'] },
      ],
    }];

    expect(deriveModelFromCatalogs({
      provider: 'openai', client: 'codex_app_server', family: 'sol',
      effort: 'medium', seedModel: 'gpt-6-sol',
    }, catalogs)).toEqual({ model: 'gpt-5.6-sol', source: 'catalog' });
    const complex = defaultRolePreferences(catalogs).find(p => p.purpose === 'complex_implementation');
    expect(complex?.preferred_route.model).toBe('gpt-5.6-sol');
  });

  test('ignores stale, configured, unavailable, unknown-effort and misleading family records for defaults', () => {
    const target = { provider: 'openai', client: 'codex_app_server', family: 'sol', effort: 'medium' as const, seedModel: 'gpt-6-sol' };
    const make = (source: 'provider' | 'configured', stale: boolean, status: 'available' | 'unavailable', id: string, efforts: Array<'medium'> = ['medium']): SubscriptionModelCatalogView => ({
      provider: 'openai', client: 'codex_app_server', source, stale, status,
      observed_at: '2026-09-29T18:00:00Z', message: '', models: [{ id, label: id, efforts }],
    });
    const catalogs = [
      make('provider', true, 'available', 'gpt-20-sol'),
      make('configured', false, 'available', 'gpt-19-sol'),
      make('provider', false, 'unavailable', 'gpt-18-sol'),
      make('provider', false, 'available', 'gpt-17-sol-experimental'),
      make('provider', false, 'available', 'gpt-16-sol', []),
      { ...make('provider', false, 'available', 'gpt-7-sol'), observed_at: '2026-09-29T19:00:00Z' },
    ];
    expect(deriveModelFromCatalogs(target, catalogs)).toEqual({ model: 'gpt-7-sol', source: 'catalog' });
    expect(deriveModelFromCatalogs(target, catalogs.slice(0, 5))).toEqual({ model: 'gpt-6-sol', source: 'seed' });
    expect(deriveModelFromCatalogs({ ...target, client: 'other' }, catalogs).source).toBe('seed');
  });
});

describe('profile editor catalog choices and interactions', () => {
  test('model presets span providers and atomically select a complete route', async () => {
    render(<ProfileEditor catalogPage={catalogPage} onSave={vi.fn()} />);
    const modelChoiceSelect = screen.getByLabelText('Primary model');
    const gemini = Array.from((modelChoiceSelect as HTMLSelectElement).options).find(option => option.textContent?.includes('Gemini 4 Flash'))!;
    expect(Array.from((modelChoiceSelect as HTMLSelectElement).options).some(option => option.textContent?.includes('GPT-7 Sol'))).toBe(true);
    await userEvent.selectOptions(modelChoiceSelect, gemini.value);
    await userEvent.click(screen.getAllByText('Advanced')[0]);
    expect(screen.getByLabelText('Primary provider')).toHaveValue('google');
    expect(screen.getByLabelText('Primary client')).toHaveValue('gemini_cli');
    expect(screen.getByLabelText('Primary custom model')).toHaveValue('gemini-4-flash');
    expect(screen.getByLabelText('Primary reasoning')).toHaveValue('medium');
  });

  test('catalog presets distinguish the same model id across provider and client', () => {
    const choices = [
      { provider: 'openai', client: 'codex', model: 'same-id', effort: 'low' as const },
      { provider: 'google', client: 'gemini', model: 'same-id', effort: 'low' as const },
    ];
    expect(new Set(choices.map(routePresetIdentity)).size).toBe(2);
    const sameProvider = [
      { provider: 'google', client: 'gemini_cli', model: 'same-id', effort: 'low' as const },
      { provider: 'google', client: 'other_google_client', model: 'same-id', effort: 'low' as const },
    ];
    expect(new Set(sameProvider.map(routePresetIdentity)).size).toBe(2);
  });

  test('selects and saves the same model id from a different client while preserving supported reasoning', async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    const catalogs: SubscriptionModelCatalogView[] = [
      { provider: 'google', client: 'client_a', source: 'provider', status: 'available', observed_at: '2026-10-01T00:00:00Z', stale: false, models: [{ id: 'same-id', label: 'Same model', efforts: ['medium', 'high'] }], message: '' },
      { provider: 'google', client: 'client_b', source: 'provider', status: 'available', observed_at: '2026-10-01T00:00:00Z', stale: false, models: [{ id: 'same-id', label: 'Same model', efforts: ['medium'] }], message: '' },
    ];
    render(<ProfileEditor catalogPage={{ catalogs, observed_at: '2026-10-01T00:00:00Z' }} onSave={save} />);
    const model = screen.getByLabelText('Primary model') as HTMLSelectElement;
    const clientA = Array.from(model.options).find(option => option.textContent?.includes('client a'))!;
    await userEvent.selectOptions(model, clientA.value);
    await userEvent.selectOptions(screen.getByLabelText('Primary reasoning'), 'medium');
    const clientB = Array.from(model.options).find(option => option.textContent?.includes('client b'))!;
    await userEvent.selectOptions(model, clientB.value);
    expect(screen.getByLabelText('Primary reasoning')).toHaveValue('medium');
    await userEvent.click(screen.getByRole('button', { name: 'Create profile version 1' }));
    expect(save.mock.calls[0][0].preferences[0].preferred_route).toEqual({
      provider: 'google', client: 'client_b', model: 'same-id', effort: 'medium',
      auth_mode: 'subscription', billing_mode: 'allowance_only',
    });
  });

  test('allows explicit custom entry and preserves custom saved values when obsolete', async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    // Initial profile has an obsolete uncataloged model (gpt-5.6-terra)
    const initial = {
      profile_id: 'legacy-profile',
      version: 1,
      default_billing_mode: 'allowance_only' as const,
      approved_mappings: [],
      preferences: [
        {
          purpose: 'primary' as const,
          preferred_route: {
            provider: 'openai',
            client: 'codex_app_server',
            model: 'gpt-6-astra',
            effort: 'low' as const,
            auth_mode: 'subscription' as const,
            billing_mode: 'allowance_only' as const,
          },
          fallback_routes: [],
        },
        {
          purpose: 'complex_implementation' as const,
          preferred_route: {
            provider: 'openai',
            client: 'codex_app_server',
            model: 'gpt-5.6-terra',
            effort: 'low' as const,
            auth_mode: 'subscription' as const,
            billing_mode: 'allowance_only' as const,
          },
          fallback_routes: [],
        },
      ],
    };

    render(
      <ProfileEditor
        initial={initial}
        expectedVersion={1}
        catalogPage={catalogPage}
        onSave={save}
      />
    );

    const role2ModelInput = screen.getByLabelText('Complex implementer custom model');
    expect(role2ModelInput).toHaveValue('gpt-5.6-terra');

    // User can type an arbitrary custom model
    await userEvent.clear(role2ModelInput);
    await userEvent.type(role2ModelInput, 'custom-inhouse-model-v1');
    expect(role2ModelInput).toHaveValue('custom-inhouse-model-v1');

    await userEvent.click(screen.getByRole('button', { name: 'Append version 2' }));
    expect(save).toHaveBeenCalledWith(
      expect.objectContaining({
        preferences: [
          expect.objectContaining({
            purpose: 'primary',
          }),
          expect.objectContaining({
            purpose: 'complex_implementation',
            preferred_route: expect.objectContaining({
              model: 'custom-inhouse-model-v1',
            }),
          }),
        ],
      })
    );
  });

  test('fallback routes offer model choices and custom entry', async () => {
    render(<ProfileEditor catalogPage={catalogPage} onSave={vi.fn()} />);
    await userEvent.click(screen.getAllByText('Advanced')[0]);
    await userEvent.click(screen.getAllByRole('button', { name: 'Add fallback route' })[0]);
    const fbModelChoice = screen.getByLabelText('Primary fallback route 1 model');
    const gemini = Array.from((fbModelChoice as HTMLSelectElement).options).find(option => option.textContent?.includes('Gemini 4 Flash'))!;
    await userEvent.selectOptions(fbModelChoice, gemini.value);
    expect(screen.getByLabelText('Primary fallback route 1 custom model')).toHaveValue('gemini-4-flash');
  });

  test('changing role purpose does not remount role section or destroy focus/draft', async () => {
    render(<ProfileEditor catalogPage={catalogPage} onSave={vi.fn()} />);

    await userEvent.click(screen.getAllByText('Advanced')[0]);
    const modelInput = screen.getByLabelText('Primary custom model');
    await userEvent.clear(modelInput);
    await userEvent.type(modelInput, 'draft-in-progress');

    // Change purpose from primary to security
    const purposeSelect = screen.getByLabelText('Purpose 1');
    purposeSelect.focus();
    expect(purposeSelect).toHaveFocus();

    await userEvent.selectOptions(purposeSelect, 'security');

    // Focus remains on purposeSelect and draft input text is preserved!
    expect(purposeSelect).toHaveFocus();
    expect(screen.getAllByLabelText('Security reviewer custom model')[0]).toHaveValue('draft-in-progress');
  });

  test('handles late catalog load without overwriting existing draft edits', async () => {
    let resolveCatalogs!: (page: SubscriptionModelCatalogPage) => void;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (url.includes('/subscription-models')) {
          return new Promise(done => {
            resolveCatalogs = (page: SubscriptionModelCatalogPage) =>
              done(new Response(JSON.stringify(page)));
          });
        }
        return Promise.resolve(new Response(JSON.stringify({})));
      })
    );

    render(<ProfileEditor onSave={vi.fn()} />);

    // User starts typing before catalogs arrive
    await userEvent.click(screen.getAllByText('Advanced')[0]);
    const modelInput = screen.getByLabelText('Primary custom model');
    await userEvent.clear(modelInput);
    await userEvent.type(modelInput, 'user-typed-draft');

    // Now catalogs arrive late
    resolveCatalogs(catalogPage);

    // Verify draft was NOT overwritten
    await waitFor(() => {
      expect(screen.getByLabelText('Primary custom model')).toHaveValue('user-typed-draft');
    });
  });

  test('failed catalog refresh shows error with retry without breaking custom entry', async () => {
    let failRefresh!: (error: Error) => void;
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(catalogPage)))
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { failRefresh = reject; }))
      .mockResolvedValueOnce(new Response(JSON.stringify(catalogPage)));
    vi.stubGlobal('fetch', fetcher);
    render(<ProfileEditor onSave={vi.fn()} />);
    await waitFor(() => expect(Array.from((screen.getByLabelText('Primary model') as HTMLSelectElement).options).some(option => option.textContent?.includes('GPT-7 Sol'))).toBe(true));
    await userEvent.click(screen.getAllByText('Advanced')[0]);
    const model = screen.getByLabelText('Primary custom model');
    await userEvent.clear(model);
    await userEvent.type(model, 'custom-draft');
    const refreshButton = screen.getByRole('button', { name: 'Refresh model choices' });
    await userEvent.click(refreshButton);
    expect(refreshButton).toBeDisabled();
    await act(async () => { failRefresh(new Error('offline')); });
    expect(screen.getByRole('alert')).toHaveTextContent('Model choices unavailable');
    expect(Array.from((screen.getByLabelText('Primary model') as HTMLSelectElement).options).some(option => option.textContent?.includes('GPT-7 Sol'))).toBe(true);
    expect(model).toHaveValue('custom-draft');
    expect(screen.getByRole('alert')).toHaveTextContent('Model choices unavailable');
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(model).toHaveValue('custom-draft');
    expect(fetcher).toHaveBeenCalledTimes(3);
  });
});

describe('readiness status layout stability', () => {
  test('readiness refresh retains fixed layout status region to avoid page jump', async () => {
    let completeRefresh!: (response: Response) => void;
    const report = new Response(JSON.stringify({
      observed_at: '2026-09-29T18:00:00Z', fresh_for_seconds: 45, has_more: false,
      workers: [{ worker_instance_id: 'w1', state: 'current', last_seen_at: '2026-09-29T18:00:00Z', stopped_at: null, routes: [] }],
    }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValueOnce(report)
        .mockImplementationOnce(() => new Promise(resolve => { completeRefresh = resolve; }))
    );

    render(<SubscriptionRuntimeStatus />);
    await screen.findByText(/No subscription routes registered by this worker/);
    const statusRegion = document.querySelector('.runtime-refresh-status');
    expect(statusRegion).toBeInTheDocument();
    expect(statusRegion).toHaveAttribute('role', 'status');
    expect(statusRegion).toHaveStyle({ minHeight: '1.5rem' });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh subscription readiness' }));
    expect(screen.getByText('Refreshing worker registration…')).toBeInTheDocument();
    expect(document.querySelector('.runtime-refresh-status')).toBe(statusRegion);
    await act(async () => { completeRefresh(new Response(JSON.stringify({
      observed_at: '2026-09-29T18:01:00Z', fresh_for_seconds: 45, has_more: false,
      workers: [{ worker_instance_id: 'w1', state: 'current', last_seen_at: '2026-09-29T18:01:00Z', stopped_at: null, routes: [] }],
    }))); });
    expect(document.querySelector('.runtime-refresh-status')).toBe(statusRegion);
    expect(screen.queryByText('Refreshing worker registration…')).not.toBeInTheDocument();
  });
});
