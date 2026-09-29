import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, test, vi } from 'vitest';
import { ProfileEditor } from './profile-editor';
import { defaultRolePreferences, extractNumericVersion, compareNumericVersions, deriveModelFromCatalogs } from './models';
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
  test('model choices are scoped to provider + client and display freshness', async () => {
    render(<ProfileEditor catalogPage={catalogPage} onSave={vi.fn()} />);

    // Initially route is blank; set provider and client
    const providerInput = screen.getByLabelText('Preferred route provider');
    const clientInput = screen.getByLabelText('Preferred route client');
    await userEvent.type(providerInput, 'openai');
    await userEvent.type(clientInput, 'codex_app_server');

    // Metadata displays source and freshness
    expect(screen.getByText('Catalog source:')).toBeInTheDocument();
    expect(screen.getByText('Freshness:')).toBeInTheDocument();
    expect(screen.getByText(/Current provider catalog choices/i)).toBeInTheDocument();

    // Model choices select has OpenAI Codex models
    const modelChoiceSelect = screen.getByLabelText('Preferred route model choice');
    expect(modelChoiceSelect).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /GPT-7 Sol/i })).toBeInTheDocument();

    // Selecting a model updates the model input
    await userEvent.selectOptions(modelChoiceSelect, 'gpt-7-sol');
    expect(screen.getByLabelText('Preferred route model')).toHaveValue('gpt-7-sol');
  });

  test('switching provider/client filters model choices dynamically', async () => {
    render(<ProfileEditor catalogPage={catalogPage} onSave={vi.fn()} />);

    const providerInput = screen.getByLabelText('Preferred route provider');
    const clientInput = screen.getByLabelText('Preferred route client');

    await userEvent.type(providerInput, 'anthropic');
    await userEvent.type(clientInput, 'claude_code');

    const modelChoiceSelect = screen.getByLabelText('Preferred route model choice');
    expect(screen.getByRole('option', { name: /Claude Opus 6/i })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /GPT-7 Sol/i })).not.toBeInTheDocument();
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

    // The obsolete saved model is preserved in the input and select
    const role2ModelInput = screen.getAllByLabelText('Preferred route model')[1];
    expect(role2ModelInput).toHaveValue('gpt-5.6-terra');

    const role2ModelChoiceSelect = screen.getAllByLabelText('Preferred route model choice')[1];
    expect(role2ModelChoiceSelect).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Custom \/ uncataloged: gpt-5.6-terra/i })).toBeInTheDocument();

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

    await userEvent.click(screen.getByRole('button', { name: 'Add fallback route' }));

    const fbProvider = screen.getByLabelText('Fallback route 1 provider');
    const fbClient = screen.getByLabelText('Fallback route 1 client');
    await userEvent.type(fbProvider, 'google');
    await userEvent.type(fbClient, 'gemini_cli');

    const fbModelChoice = screen.getByLabelText('Fallback route 1 model choice');
    expect(fbModelChoice).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Gemini 4 Flash/i })).toBeInTheDocument();

    await userEvent.selectOptions(fbModelChoice, 'gemini-4-flash');
    expect(screen.getByLabelText('Fallback route 1 model')).toHaveValue('gemini-4-flash');
  });

  test('changing role purpose does not remount role section or destroy focus/draft', async () => {
    render(<ProfileEditor catalogPage={catalogPage} onSave={vi.fn()} />);

    const providerInput = screen.getByLabelText('Preferred route provider');
    await userEvent.type(providerInput, 'openai');
    const modelInput = screen.getByLabelText('Preferred route model');
    await userEvent.type(modelInput, 'draft-in-progress');

    // Change purpose from primary to security
    const purposeSelect = screen.getByLabelText('Purpose 1');
    purposeSelect.focus();
    expect(purposeSelect).toHaveFocus();

    await userEvent.selectOptions(purposeSelect, 'security');

    // Focus remains on purposeSelect and draft input text is preserved!
    expect(purposeSelect).toHaveFocus();
    expect(screen.getByLabelText('Preferred route model')).toHaveValue('draft-in-progress');
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
    const modelInput = screen.getByLabelText('Preferred route model');
    await userEvent.type(modelInput, 'user-typed-draft');

    // Now catalogs arrive late
    resolveCatalogs(catalogPage);

    // Verify draft was NOT overwritten
    await waitFor(() => {
      expect(screen.getByLabelText('Preferred route model')).toHaveValue('user-typed-draft');
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
    await userEvent.type(screen.getByLabelText('Preferred route provider'), 'openai');
    await userEvent.type(screen.getByLabelText('Preferred route client'), 'codex_app_server');
    await screen.findByRole('option', { name: /GPT-7 Sol/i });
    const model = screen.getByLabelText('Preferred route model');
    await userEvent.type(model, 'custom-draft');
    const refreshButton = screen.getByRole('button', { name: 'Refresh model choices' });
    await userEvent.click(refreshButton);
    expect(refreshButton).toBeDisabled();
    await act(async () => { failRefresh(new Error('offline')); });
    expect(screen.getByRole('alert')).toHaveTextContent('Model choices unavailable');
    expect(screen.getByRole('option', { name: /GPT-7 Sol/i })).toBeInTheDocument();
    expect(model).toHaveValue('custom-draft');
    expect(screen.getByLabelText('Preferred route catalog status')).toHaveTextContent('unverified');
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
