'use client';

import { useId, useMemo, useState } from 'react';
import type { components } from '@/lib/api/schema';
import { useApi } from '@/hooks/use-api';
import { Button } from '@/components/ui/button';
import { ProviderBadge, getProviderCue } from '@/components/ui/provider-badge';
import { profileLabel } from './labels';
import {
  defaultRolePreferences,
  offlineRoleSeeds,
  selectCatalogForRoute,
  type ReasoningEffort,
  type Route,
  type Preference,
  type SubscriptionModelCatalogPage,
  type SubscriptionModelCatalogView,
  type SubscriptionModelOption,
} from './models';
import { JevSettings, type JevPolicy } from '@/components/projects/jev-settings';

type ProfileCreate = components['schemas']['ProfileCreateRequest'];
type ProfileAppend = components['schemas']['ProfileAppendRequest'];
type Profile = components['schemas']['ProfileResponse'];
type Mapping = components['schemas']['MappingInput'];

interface EditablePreference extends Preference {
  id: string;
}

const purposes: components['schemas']['SpecialistPurpose'][] = [
  'primary',
  'routine_implementation',
  'complex_implementation',
  'independent_review',
  'planning',
  'exploration',
  'security',
  'integration',
  'verification',
];

const efforts: ReasoningEffort[] = [
  'none',
  'low',
  'medium',
  'high',
  'maximum',
];

const defaultRoute = (): Route => ({
  provider: '',
  client: '',
  model: '',
  effort: 'low',
  auth_mode: 'subscription',
  billing_mode: 'allowance_only',
});

const defaultPreference = (purpose: Preference['purpose'] = 'primary'): Preference => ({
  purpose,
  preferred_route: defaultRoute(),
  fallback_routes: [],
});

const seedRoutes = offlineRoleSeeds.flatMap(seed => [seed.preferred, ...seed.fallbacks]);
const seedModelIds = new Set(seedRoutes.map(route => route.model));

function RouteFields({
  route,
  label,
  update,
  catalogs,
  catalogLoading,
  catalogFailed,
}: {
  route: Route;
  label: string;
  update: (change: Partial<Route>) => void;
  catalogs?: SubscriptionModelCatalogView[];
  catalogLoading?: boolean;
  catalogFailed?: boolean;
}) {
  const cue = getProviderCue(route.provider);
  const suggestionId = useId();

  // Derive known providers across catalogs and offline seeds
  const knownProviders = useMemo(() => {
    const set = new Set(seedRoutes.map(route => route.provider));
    catalogs?.forEach(c => {
      if (c.provider) set.add(c.provider);
    });
    return Array.from(set).sort();
  }, [catalogs]);

  // Derive available clients for the selected provider
  const availableClients = useMemo(() => {
    const providerLower = route.provider.trim().toLowerCase();
    if (!providerLower) return [];
    const set = new Set<string>();
    seedRoutes.forEach(seed => {
      if (seed.provider.toLowerCase() === providerLower) set.add(seed.client);
    });
    catalogs?.forEach(c => {
      if (c.provider.toLowerCase() === providerLower && c.client) set.add(c.client);
    });
    return Array.from(set).sort();
  }, [catalogs, route.provider]);

  const activeCatalog = useMemo(() => {
    return selectCatalogForRoute(catalogs, route.provider, route.client);
  }, [catalogs, route.provider, route.client]);

  const catalogModels: SubscriptionModelOption[] = activeCatalog?.models ?? [];
  const isCatalogModel = catalogModels.some(m => m.id === route.model);
  const isSeed = seedModelIds.has(route.model.trim());
  const currentProviderReport = activeCatalog?.source === 'provider' &&
    activeCatalog.status === 'available' && !activeCatalog.stale &&
    !!activeCatalog.observed_at && Number.isFinite(Date.parse(activeCatalog.observed_at));

  return (
    <fieldset className="card-provider" data-provider={cue.tone} style={{ margin: '12px 0' }}>
      <legend style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <span>{label}</span>
        {route.provider.trim() && <ProviderBadge provider={route.provider} />}
        {isCatalogModel ? (
          <span className="meta text-xs" style={{ fontWeight: 'normal' }}>
            ({catalogFailed ? 'Retained catalog / unverified' :
              currentProviderReport ? 'Current provider report' :
              activeCatalog?.source === 'configured' ? 'Configured choice / unverified' :
                'Historical provider report / unverified'})
          </span>
        ) : isSeed ? (
          <span className="meta text-xs" style={{ fontWeight: 'normal' }}>
            (Suggestion / unverified)
          </span>
        ) : null}
      </legend>

      {activeCatalog ? (
        <div
          className="catalog-metadata meta text-xs"
          aria-label={`${label} catalog status`}
          style={{ marginBottom: '8px' }}
        >
          <span>
            Catalog source: <strong>{activeCatalog.source}</strong>
          </span>{' '}
          ·{' '}
          <span>
            Status: <strong>{activeCatalog.status}</strong>
          </span>{' '}
          ·{' '}
          <span>
            Freshness: <strong>{activeCatalog.stale ? 'Stale' : 'Current'}</strong>
          </span>
          {activeCatalog.observed_at ? (
            <>
              {' '}
              · Observed: <time dateTime={activeCatalog.observed_at}>{activeCatalog.observed_at}</time>
            </>
          ) : null}
          {activeCatalog.message ? <> · {activeCatalog.message}</> : null}
          {catalogFailed ? <> · Latest catalog refresh failed; retained choices are unverified.</> : null}
        </div>
      ) : route.provider && route.client && !catalogLoading ? (
        <p className="meta text-xs" style={{ marginBottom: '8px' }}>
          {catalogFailed
            ? 'Model catalog unavailable for this provider and client. Custom model entry is enabled.'
            : 'No catalog report for this provider and client. Custom model entry is enabled.'}
        </p>
      ) : null}

      <p className="meta text-xs" style={{ margin: '0 0 10px' }}>
        Model choices are advisory; they do not guarantee admission or remaining quota.
      </p>

      <div className="route-fields-grid">
        <div className="form-field">
          <label>
            Provider
            <input
              aria-label={`${label} provider`}
              value={route.provider}
              onChange={event => update({ provider: event.target.value })}
              list={`${suggestionId}-providers`}
              placeholder="e.g. openai, google, anthropic"
            />
          </label>
          <datalist id={`${suggestionId}-providers`}>{knownProviders.map(p => <option key={p} value={p} />)}</datalist>
        </div>

        <div className="form-field">
          <label>
            Client
            <input
              aria-label={`${label} client`}
              value={route.client}
              onChange={event => update({ client: event.target.value })}
              list={`${suggestionId}-clients`}
              placeholder="e.g. codex_app_server, gemini_cli"
            />
          </label>
          <datalist id={`${suggestionId}-clients`}>{availableClients.map(c => <option key={c} value={c} />)}</datalist>
        </div>

        <div className="form-field">
          <label>
            Requested model
            <input
              aria-label={`${label} model`}
              value={route.model}
              onChange={event => update({ model: event.target.value })}
              placeholder="e.g. gpt-6-astra"
            />
          </label>
          <label className="meta text-xs" style={{ marginTop: '4px', display: 'block' }}>
            Model choices
            <select
              aria-label={`${label} model choice`}
              value={route.model}
              onChange={event => {
                const val = event.target.value;
                if (!val) return;
                const chosen = catalogModels.find(m => m.id === val);
                if (chosen) {
                  // If chosen model restricts efforts and current effort isn't valid, adjust effort
                  const newEffort =
                    chosen.efforts && chosen.efforts.length > 0 && !chosen.efforts.includes(route.effort)
                      ? chosen.efforts[0]
                      : route.effort;
                  update({ model: chosen.id, effort: newEffort });
                } else {
                  update({ model: val });
                }
              }}
            >
              <option value="">
                {catalogLoading
                  ? 'Loading model choices…'
                  : catalogModels.length > 0
                  ? 'Choose a model…'
                  : 'No catalog models (custom entry enabled)'}
              </option>
              {catalogModels.map(m => (
                <option key={m.id} value={m.id}>
                  {m.label || m.id}
                  {m.efforts.length ? ` (${m.efforts.join(', ')})` : ''}
                </option>
              ))}
              {route.model && !isCatalogModel && (
                <option value={route.model}>
                  Custom / uncataloged: {route.model}
                </option>
              )}
            </select>
          </label>
        </div>

        <div className="form-field">
          <label>
            Effort
            <select
              aria-label={`${label} effort`}
              value={route.effort}
              onChange={event => update({ effort: event.target.value as Route['effort'] })}
            >
              {efforts.map(value => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
        </div>

        <div className="form-field">
          <label>
            Authentication
            <select
              aria-label={`${label} authentication`}
              value={route.auth_mode}
              onChange={event => update({ auth_mode: event.target.value as Route['auth_mode'] })}
            >
              <option value="subscription">Subscription</option>
              <option value="api_key">API key</option>
            </select>
          </label>
        </div>

        <div className="form-field">
          <label>
            Billing
            <select
              aria-label={`${label} billing`}
              value={route.billing_mode}
              onChange={event => update({ billing_mode: event.target.value as Route['billing_mode'] })}
            >
              <option value="allowance_only">Allowance only</option>
              <option value="paid_opt_in">Paid opt-in</option>
            </select>
          </label>
        </div>
      </div>
    </fieldset>
  );
}

export function ProfileEditor({
  initial,
  expectedVersion,
  onSave,
  catalogPage,
  onRefreshCatalogs,
  saveUnavailable = false,
}: {
  initial?: Profile;
  expectedVersion?: number;
  onSave: (request: ProfileCreate | ProfileAppend) => Promise<unknown>;
  catalogPage?: SubscriptionModelCatalogPage;
  onRefreshCatalogs?: () => void;
  saveUnavailable?: boolean;
}) {
  const fallbackId = useId();

  // Fetch catalogs with stable retention on refresh
  const fetchedCatalogs = useApi<SubscriptionModelCatalogPage>('/subscription-models', {
    keepPreviousOnRefresh: true,
    keepPreviousOnError: true,
  });

  const catalogs = catalogPage
    ? {
        value: catalogPage,
        loading: false,
        failed: false,
        refreshing: false,
        refresh: onRefreshCatalogs ?? (() => {}),
      }
    : fetchedCatalogs;

  const [preferences, setPreferences] = useState<EditablePreference[]>(() =>
    (initial?.preferences ?? [defaultPreference()]).map((value, i) => ({
      id: `role-${i}-${fallbackId}`,
      purpose: value.purpose as Preference['purpose'],
      preferred_route: value.preferred_route as Route,
      fallback_routes: (value.fallback_routes as Route[]) ?? [],
    }))
  );

  const [billing, setBilling] = useState<components['schemas']['BillingMode']>(
    (initial?.default_billing_mode as components['schemas']['BillingMode']) ?? 'allowance_only'
  );

  const [mappings, setMappings] = useState<Mapping[]>(
    () =>
      (initial?.approved_mappings as Mapping[])?.map(value => ({
        requested_model: String(value.requested_model ?? ''),
        effective_model: String(value.effective_model ?? ''),
        reason: String(value.reason ?? ''),
      })) ?? []
  );

  const [jev, setJev] = useState<JevPolicy | null | undefined>(
    () => (initial?.jev ? { ...initial.jev } : undefined)
  );

  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);

  const updatePreference = (index: number, update: Partial<Preference>) =>
    setPreferences(current =>
      current.map((item, itemIndex) => (itemIndex === index ? { ...item, ...update } : item))
    );

  const updateRoute = (
    index: number,
    fallbackIndex: number | undefined,
    update: Partial<Route>
  ) =>
    setPreferences(current =>
      current.map((item, itemIndex) =>
        itemIndex === index
          ? {
              ...item,
              ...(fallbackIndex === undefined
                ? { preferred_route: { ...item.preferred_route, ...update } }
                : {
                    fallback_routes: item.fallback_routes.map((r, routeIndex) =>
                      routeIndex === fallbackIndex ? { ...r, ...update } : r
                    ),
                  }),
            }
          : item
      )
    );

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(undefined);
    const purposesSeen = new Set(preferences.map(item => item.purpose));
    if (!preferences.length || !purposesSeen.has('primary')) {
      setError('Add exactly one primary role preference before saving.');
      return;
    }
    if (purposesSeen.size !== preferences.length) {
      setError('Each role purpose must be unique.');
      return;
    }
    const routes = preferences.flatMap(item => [item.preferred_route, ...item.fallback_routes]);
    if (
      routes.some(
        r => !r.provider.trim() || !r.client.trim() || !r.model.trim()
      )
    ) {
      setError('Every route needs a provider, client, and requested model.');
      return;
    }
    if (
      mappings.some(
        mapping =>
          !mapping.requested_model.trim() ||
          !mapping.effective_model.trim() ||
          !mapping.reason.trim()
      )
    ) {
      setError('Every approved mapping needs a requested model, effective model, and reason.');
      return;
    }

    setSaving(true);
    try {
      // Strip internal id property when submitting
      const sanitizedPreferences: Preference[] = preferences.map(
        ({ id: _id, ...rest }) => rest
      );
      const payload: ProfileCreate | ProfileAppend = {
        preferences: sanitizedPreferences,
        approved_mappings: mappings,
        default_billing_mode: billing,
        ...(expectedVersion === undefined
          ? {}
          : { expected_current_version: expectedVersion }),
        ...(jev != null
          ? { jev }
          : expectedVersion !== undefined && initial?.jev != null
          ? { jev: null }
          : {}),
      };
      await onSave(payload);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Profile could not be saved.');
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      className="editor-card"
      aria-label={
        expectedVersion === undefined
          ? 'Create subscription profile'
          : `Append profile version ${expectedVersion}`
      }
    >
      <p className="meta" style={{ margin: '0 0 16px' }}>
        Routes are requested configuration. Effective support is verified separately by the signed-in client.
      </p>

      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '8px', marginBottom: '16px' }}>
        {!initial && expectedVersion === undefined && (
          <Button
            type="button"
            onClick={() => {
              const defaults = defaultRolePreferences(catalogs.failed ? undefined : catalogs.value?.catalogs);
              setPreferences(
                defaults.map((p, i) => ({
                  ...p,
                  id: `role-${i}-${crypto.randomUUID()}`,
                }))
              );
            }}
          >
            Use default role preferences
          </Button>
        )}
        <Button
          type="button"
          variant="secondary"
          disabled={catalogs.refreshing}
          onClick={catalogs.refresh}
        >
          Refresh model choices
        </Button>
      </div>

      {catalogs.loading && <p role="status">Loading model choices…</p>}
      {catalogs.failed && (
        <p role="alert" className="field-error">
          Model choices unavailable.{' '}
          <Button type="button" variant="quiet" onClick={catalogs.refresh}>
            Retry
          </Button>
        </p>
      )}

      {preferences.map((preference, index) => (
        <section
          key={preference.id}
          className="editor-role-section"
        >
          <div
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: '12px',
              marginBottom: '12px',
            }}
          >
            <h3 style={{ margin: 0 }}>
              {profileLabel(preference.purpose)}{' '}
              <span className="meta">· Role {index + 1}</span>
            </h3>
            {preferences.length > 1 && (
              <Button
                type="button"
                variant="danger"
                onClick={() =>
                  setPreferences(current => current.filter((_, itemIndex) => itemIndex !== index))
                }
              >
                Remove role
              </Button>
            )}
          </div>

          <div className="form-field" style={{ maxWidth: '320px', marginBottom: '12px' }}>
            <label>
              Purpose
              <select
                aria-label={`Purpose ${index + 1}`}
                value={preference.purpose}
                onChange={event =>
                  updatePreference(index, { purpose: event.target.value as Preference['purpose'] })
                }
              >
                {purposes.map(value => (
                  <option key={value} value={value}>
                    {profileLabel(value)}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <RouteFields
            label="Preferred route"
            route={preference.preferred_route}
            update={change => updateRoute(index, undefined, change)}
            catalogs={catalogs.value?.catalogs}
            catalogLoading={catalogs.loading}
            catalogFailed={catalogs.failed}
          />

          <div style={{ marginTop: '16px' }}>
            <h4 style={{ margin: '0 0 8px' }}>Explicit fallback routes</h4>
            {preference.fallback_routes.map((r, fallbackIndex) => (
              <div
                key={fallbackIndex}
                style={{
                  background: 'var(--surface)',
                  border: '1px solid var(--border)',
                  borderRadius: '6px',
                  padding: '12px',
                  marginBottom: '10px',
                }}
              >
                <RouteFields
                  label={`Fallback route ${fallbackIndex + 1}`}
                  route={r}
                  update={change => updateRoute(index, fallbackIndex, change)}
                  catalogs={catalogs.value?.catalogs}
                  catalogLoading={catalogs.loading}
                  catalogFailed={catalogs.failed}
                />
                <Button
                  type="button"
                  variant="danger"
                  onClick={() =>
                    setPreferences(current =>
                      current.map((item, itemIndex) =>
                        itemIndex === index
                          ? {
                              ...item,
                              fallback_routes: item.fallback_routes.filter(
                                (_, routeIndex) => routeIndex !== fallbackIndex
                              ),
                            }
                          : item
                      )
                    )
                  }
                >
                  Remove fallback
                </Button>
              </div>
            ))}

            <Button
              type="button"
              variant="secondary"
              onClick={() =>
                setPreferences(current =>
                  current.map((item, itemIndex) =>
                    itemIndex === index
                      ? {
                          ...item,
                          fallback_routes: [...item.fallback_routes, defaultRoute()],
                        }
                      : item
                  )
                )
              }
            >
              Add fallback route
            </Button>
          </div>
        </section>
      ))}

      <div style={{ margin: '16px 0' }}>
        <Button
          type="button"
          variant="secondary"
          onClick={() =>
            setPreferences(current => [
              ...current,
              {
                ...defaultPreference('routine_implementation'),
                id: `role-${current.length}-${crypto.randomUUID()}`,
              },
            ])
          }
        >
          Add role preference
        </Button>
      </div>

      <div className="form-field" style={{ maxWidth: '320px', margin: '20px 0' }}>
        <label>
          Default billing mode
          <select
            aria-label="Default billing mode"
            value={billing}
            onChange={event =>
              setBilling(event.target.value as components['schemas']['BillingMode'])
            }
          >
            <option value="allowance_only">Allowance only</option>
            <option value="paid_opt_in">Paid opt-in</option>
          </select>
        </label>
      </div>

      <fieldset style={{ margin: '20px 0' }}>
        <legend>Approved mappings</legend>
        <p className="meta" style={{ margin: '0 0 12px' }}>
          Translate requested models into effective models when admitted.
        </p>

        {mappings.map((mapping, index) => (
          <div key={index} className="mapping-row">
            <div className="form-field">
              <label>
                Requested model
                <input
                  aria-label={`Requested mapping ${index + 1}`}
                  placeholder="Requested model"
                  value={mapping.requested_model}
                  onChange={event =>
                    setMappings(current =>
                      current.map((item, itemIndex) =>
                        itemIndex === index
                          ? { ...item, requested_model: event.target.value }
                          : item
                      )
                    )
                  }
                />
              </label>
            </div>

            <div className="form-field">
              <label>
                Effective model
                <input
                  aria-label={`Effective mapping ${index + 1}`}
                  placeholder="Effective model"
                  value={mapping.effective_model}
                  onChange={event =>
                    setMappings(current =>
                      current.map((item, itemIndex) =>
                        itemIndex === index
                          ? { ...item, effective_model: event.target.value }
                          : item
                      )
                    )
                  }
                />
              </label>
            </div>

            <div className="form-field">
              <label>
                Reason
                <input
                  aria-label={`Mapping reason ${index + 1}`}
                  placeholder="Reason"
                  value={mapping.reason}
                  onChange={event =>
                    setMappings(current =>
                      current.map((item, itemIndex) =>
                        itemIndex === index ? { ...item, reason: event.target.value } : item
                      )
                    )
                  }
                />
              </label>
            </div>

            <div>
              <Button
                type="button"
                variant="danger"
                onClick={() =>
                  setMappings(current => current.filter((_, itemIndex) => itemIndex !== index))
                }
              >
                Remove mapping
              </Button>
            </div>
          </div>
        ))}

        <div style={{ marginTop: '10px' }}>
          <Button
            type="button"
            variant="secondary"
            onClick={() =>
              setMappings(current => [
                ...current,
                { requested_model: '', effective_model: '', reason: '' },
              ])
            }
          >
            Add approved mapping
          </Button>
        </div>
      </fieldset>

      <div style={{ margin: '20px 0' }}>
        <JevSettings
          value={jev}
          onChange={setJev}
          checkboxLabel="Configure Jev default"
          description="Configure optional Jev advisory search and review defaults for projects using this profile. Explicit project Jev settings override this default."
        />
      </div>

      <div className="form-actions">
        {error && <p role="alert" className="field-error">{error}</p>}
        <Button type="submit" variant="primary" disabled={saving || saveUnavailable}>
          {saving
            ? 'Saving…'
            : expectedVersion === undefined
            ? 'Create profile version 1'
            : `Append version ${expectedVersion + 1}`}
        </Button>
      </div>
    </form>
  );
}

export { defaultRolePreferences };
export type { Profile, ProfileAppend, ProfileCreate, Preference, Route };
