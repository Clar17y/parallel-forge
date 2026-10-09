'use client';

import { useId, useMemo, useState } from 'react';
import type { components } from '@/lib/api/schema';
import { useApi } from '@/hooks/use-api';
import { Button } from '@/components/ui/button';
import { profileLabel } from './labels';
import {
  defaultRolePreferences,
  type Route,
  type Preference,
  type SubscriptionModelCatalogPage,
} from './models';
import { JevSettings, type JevPolicy } from '@/components/projects/jev-settings';
import { effortLabel, reasoningOptions, routePresetIdentity, routePresets, withCurrentRoute, type RoutePreset } from '@/components/model-selection/model-presets';
import styles from '@/components/model-selection/role-selector.module.css';
import { TokenBudgetSlider, tokenReference } from '@/components/model-selection/token-budget-slider';

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

const defaultRoute = (): Route => ({
  provider: '',
  client: '',
  model: '',
  effort: 'low',
  auth_mode: 'subscription',
  billing_mode: 'allowance_only',
});

function RouteFields({
  route,
  label,
  update,
  presets,
}: {
  route: Route;
  label: string;
  update: (change: Partial<Route>) => void;
  presets: RoutePreset[];
}) {
  const [customReasoning, setCustomReasoning] = useState(false);
  const choices = useMemo(() => withCurrentRoute(presets, route), [presets, route]);
  const identity = route.provider && route.client && route.model ? routePresetIdentity(route) : '';
  const selected = choices.find(choice => routePresetIdentity(choice) === identity);
  return (
    <>
      <label className={styles.choice}>
        <span className="sr-only">{label} model</span>
        <select
            aria-label={`${label} model`}
            value={identity}
            onChange={event => {
              const chosen = choices.find(
                choice => routePresetIdentity(choice) === event.target.value
              );
              if (chosen) {
                update({
                  provider: chosen.provider,
                  client: chosen.client,
                  model: chosen.model,
                  effort: chosen.efforts.includes(route.effort) ? route.effort : chosen.defaultEffort,
                });
                setCustomReasoning(false);
              }
            }}
          >
            <option value="">Choose a model…</option>
            {choices.map(choice => (
                <option key={routePresetIdentity(choice)} value={routePresetIdentity(choice)}>
                {choice.label}
              </option>
            ))}
        </select>
      </label>
      <label className={styles.reasoning}>
        <span className="reasoning-label"><span className="sr-only">{label} </span>Reasoning</span>
        <select aria-label={`${label} reasoning`} value={route.effort}
          onChange={event => {
            if (event.target.value === '__custom__') setCustomReasoning(true);
            else update({ effort: event.target.value as Route['effort'] });
          }}>
          {reasoningOptions(selected, route.effort, customReasoning).map(value => <option key={value} value={value}>{effortLabel(value)}</option>)}
          {!customReasoning && <option value="__custom__">More levels…</option>}
        </select>
      </label>
    </>
  );
}

function RouteAdvancedFields({ route, label, update }: { route: Route; label: string; update: (change: Partial<Route>) => void }) {
  return <div className="route-fields-grid">
          <label>
            Provider
            <input
              aria-label={`${label} provider`}
              value={route.provider}
              onChange={event => update({ provider: event.target.value })}
            />
          </label>
          <label>
            Client
            <input
              aria-label={`${label} client`}
              value={route.client}
              onChange={event => update({ client: event.target.value })}
            />
          </label>
          <label>
            Requested model
            <input
              aria-label={`${label} custom model`}
              value={route.model}
              onChange={event => update({ model: event.target.value })}
            />
          </label>
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
  </div>;
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

  const availableCatalogs = catalogs.value?.catalogs;
  const presets = useMemo(() => routePresets(availableCatalogs), [availableCatalogs]);

  const [preferences, setPreferences] = useState<EditablePreference[]>(() =>
    (initial?.preferences ?? defaultRolePreferences()).map((value, i) => ({
      id: `role-${i}-${fallbackId}`,
      purpose: value.purpose as Preference['purpose'],
      preferred_route: value.preferred_route as Route,
      fallback_routes: (value.fallback_routes as Route[]) ?? [],
      token_budget: value.token_budget as Preference['token_budget'],
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
    if (saving) return;
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
      aria-busy={saving}
      aria-label={
        expectedVersion === undefined
          ? 'Create subscription profile'
          : `Append profile version ${expectedVersion}`
      }
    >
      <fieldset disabled={saving} aria-label="Profile settings" style={{ border: 0, padding: 0, margin: 0, background: 'transparent', minWidth: 0 }}>
      <p className="meta" style={{ margin: '0 0 8px' }}>
        Choose a requested model; choices are advisory and do not confirm client availability or quota.
      </p>

      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
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
          className={styles.row}
        >
          <h3 className={styles.title}>{profileLabel(preference.purpose)}</h3>
          <RouteFields
            label={profileLabel(preference.purpose)}
            route={preference.preferred_route}
            update={change => updateRoute(index, undefined, change)}
            presets={presets}
          />
          <details className={styles.advanced}><summary>Advanced</summary>
            <div className="form-field" style={{ maxWidth: '320px' }}><label>Purpose
              <select aria-label={`Purpose ${index + 1}`} value={preference.purpose} onChange={event => updatePreference(index, { purpose: event.target.value as Preference['purpose'] })}>
                {purposes.map(value => <option key={value} value={value}>{profileLabel(value)}</option>)}
              </select>
            </label></div>
            <RouteAdvancedFields label={profileLabel(preference.purpose)} route={preference.preferred_route} update={change => updateRoute(index, undefined, change)} />
            <fieldset className="mt-3 space-y-4">
              <legend className="font-medium">Token budget defaults</legend>
              <p className="meta">Default token budgets for new tasks. Existing tasks keep their saved limits.</p>
              {(['input', 'output'] as const).map(dimension => {
                const key = dimension === 'input' ? 'max_input_tokens' : 'max_output_tokens';
                const budget = preference.token_budget ?? {};
                const configured = budget[key] != null;
                const reference = tokenReference(preference.preferred_route.provider, preference.preferred_route.model, dimension);
                const updateBudget = (value: number | null) => updatePreference(index, {
                  token_budget: { ...budget, [key]: value },
                });
                return <div key={dimension} className="space-y-2">
                  {configured ? <>
                    <TokenBudgetSlider
                      label={`${profileLabel(preference.purpose)} ${dimension} token budget`}
                      dimension={dimension}
                      value={budget[key]!}
                      provider={preference.preferred_route.provider}
                      model={preference.preferred_route.model}
                      onChange={value => updateBudget(value)}
                    />
                    <Button type="button" variant="quiet" aria-label={`${profileLabel(preference.purpose)} ${dimension} tokens use run/task default`} onClick={() => updateBudget(null)}>Use run/task default</Button>
                  </> : <div className="flex flex-wrap items-center justify-between gap-2">
                    <span>{dimension === 'input' ? 'Input' : 'Output'} tokens: Use run/task default</span>
                    <Button type="button" variant="secondary" aria-label={`Set ${profileLabel(preference.purpose)} ${dimension} token budget`} onClick={() => updateBudget(Math.round((reference ?? 1_000_000) / 2))}>Set explicit budget</Button>
                  </div>}
                </div>;
              })}
            </fieldset>
            {preferences.length > 1 && <Button type="button" variant="danger" onClick={() => setPreferences(current => current.filter((_, itemIndex) => itemIndex !== index))}>Remove role</Button>}
            <h4>Fallback routes ({preference.fallback_routes.length})</h4>
            {preference.fallback_routes.map((r, fallbackIndex) => <div className={styles.fallback} key={fallbackIndex}>
              <RouteFields label={`${profileLabel(preference.purpose)} fallback route ${fallbackIndex + 1}`} route={r} update={change => updateRoute(index, fallbackIndex, change)} presets={presets} />
              <RouteAdvancedFields label={`${profileLabel(preference.purpose)} fallback route ${fallbackIndex + 1}`} route={r} update={change => updateRoute(index, fallbackIndex, change)} />
              <Button type="button" variant="danger" onClick={() => setPreferences(current => current.map((item, itemIndex) => itemIndex === index ? { ...item, fallback_routes: item.fallback_routes.filter((_, routeIndex) => routeIndex !== fallbackIndex) } : item))}>Remove fallback</Button>
            </div>)}
            <Button type="button" variant="secondary" onClick={() => setPreferences(current => current.map((item, itemIndex) => itemIndex === index ? { ...item, fallback_routes: [...item.fallback_routes, defaultRoute()] } : item))}>Add fallback route</Button>
          </details>
        </section>
      ))}

      {purposes.some(purpose => !preferences.some(preference => preference.purpose === purpose)) && <div style={{ margin: '16px 0' }}>
        <Button
          type="button"
          variant="secondary"
          onClick={() =>
            setPreferences(current => {
              const usedPurposes = new Set(current.map(preference => preference.purpose));
              const purpose = purposes.find(value => !usedPurposes.has(value));
              if (!purpose) return current;
              const defaultRole = defaultRolePreferences().find(preference => preference.purpose === purpose);
              return [...current, {
                id: `role-${crypto.randomUUID()}`,
                purpose,
                preferred_route: defaultRole?.preferred_route ?? defaultRoute(),
                fallback_routes: defaultRole?.fallback_routes ?? [],
              }];
            })
          }
        >
          Add role preference
        </Button>
      </div>}

      <details style={{ margin: '20px 0' }}><summary>Advanced profile details</summary><div className="form-field" style={{ maxWidth: '320px', margin: '20px 0' }}>
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
      </details>

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
      </fieldset>
    </form>
  );
}

export { defaultRolePreferences };
export type { Profile, ProfileAppend, ProfileCreate, Preference, Route };
