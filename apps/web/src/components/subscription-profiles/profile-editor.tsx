'use client';

import { useState } from 'react';
import type { components } from '@/lib/api/schema';
import { Button } from '@/components/ui/button';
import { ProviderBadge, getProviderCue } from '@/components/ui/provider-badge';
import { profileLabel } from './labels';

type Route = components['schemas']['RouteInput'];
type Preference = components['schemas']['PreferenceInput'];
type ProfileCreate = components['schemas']['ProfileCreateRequest'];
type ProfileAppend = components['schemas']['ProfileAppendRequest'];
type Profile = components['schemas']['ProfileResponse'];
type Mapping = components['schemas']['MappingInput'];

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

const efforts: components['schemas']['ReasoningEffort'][] = [
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

const route = (
  provider: string,
  client: string,
  model: string,
  effort: Route['effort']
): Route => ({
  provider,
  client,
  model,
  effort,
  auth_mode: 'subscription',
  billing_mode: 'allowance_only',
});

export const defaultRolePreferences = (): Preference[] => [
  {
    purpose: 'primary',
    preferred_route: route('openai', 'codex_app_server', 'gpt-6-astra', 'low'),
    fallback_routes: [],
  },
  {
    purpose: 'routine_implementation',
    preferred_route: route('google', 'gemini_cli', 'gemini-3.8-flash', 'medium'),
    fallback_routes: [route('openai', 'codex_app_server', 'gpt-5.6-luna', 'medium')],
  },
  {
    purpose: 'complex_implementation',
    preferred_route: route('openai', 'codex_app_server', 'gpt-5.6-terra', 'low'),
    fallback_routes: [],
  },
  {
    purpose: 'independent_review',
    preferred_route: route('anthropic', 'claude_code', 'claude-opus-5', 'medium'),
    fallback_routes: [route('openai', 'codex_app_server', 'gpt-6-astra', 'low')],
  },
  {
    purpose: 'planning',
    preferred_route: route('openai', 'codex_app_server', 'gpt-5.6-sol', 'low'),
    fallback_routes: [],
  },
  {
    purpose: 'exploration',
    preferred_route: route('openai', 'codex_app_server', 'gpt-5.6-luna', 'medium'),
    fallback_routes: [],
  },
  {
    purpose: 'security',
    preferred_route: route('openai', 'codex_app_server', 'gpt-5.6-sol', 'high'),
    fallback_routes: [],
  },
  {
    purpose: 'integration',
    preferred_route: route('openai', 'codex_app_server', 'gpt-5.6-terra', 'low'),
    fallback_routes: [],
  },
  {
    purpose: 'verification',
    preferred_route: route('openai', 'codex_app_server', 'gpt-5.6-luna', 'medium'),
    fallback_routes: [],
  },
];

function RouteFields({
  route,
  label,
  update,
}: {
  route: Route;
  label: string;
  update: (change: Partial<Route>) => void;
}) {
  const cue = getProviderCue(route.provider);

  return (
    <fieldset className="card-provider" data-provider={cue.tone} style={{ margin: '12px 0' }}>
      <legend style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <span>{label}</span>
        {route.provider.trim() && (
          <ProviderBadge provider={route.provider} />
        )}
      </legend>

      <div className="route-fields-grid">
        <div className="form-field">
          <label>
            Provider
            <input
              aria-label={`${label} provider`}
              value={route.provider}
              onChange={event => update({ provider: event.target.value })}
              placeholder="e.g. openai, google, anthropic"
            />
          </label>
        </div>

        <div className="form-field">
          <label>
            Client
            <input
              aria-label={`${label} client`}
              value={route.client}
              onChange={event => update({ client: event.target.value })}
              placeholder="e.g. codex_app_server, gemini_cli"
            />
          </label>
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
}: {
  initial?: Profile;
  expectedVersion?: number;
  onSave: (request: ProfileCreate | ProfileAppend) => Promise<unknown>;
}) {
  const [preferences, setPreferences] = useState<Preference[]>(
    () =>
      initial?.preferences.map(value => ({
        purpose: value.purpose as Preference['purpose'],
        preferred_route: value.preferred_route as Route,
        fallback_routes: (value.fallback_routes as Route[]) ?? [],
      })) ?? [defaultPreference()]
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
                    fallback_routes: item.fallback_routes.map((route, routeIndex) =>
                      routeIndex === fallbackIndex ? { ...route, ...update } : route
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
        route => !route.provider.trim() || !route.client.trim() || !route.model.trim()
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
      await onSave({
        preferences,
        approved_mappings: mappings,
        default_billing_mode: billing,
        ...(expectedVersion === undefined
          ? {}
          : { expected_current_version: expectedVersion }),
      } as ProfileCreate | ProfileAppend);
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

      {!initial && expectedVersion === undefined && (
        <div style={{ marginBottom: '16px' }}>
          <Button type="button" onClick={() => setPreferences(defaultRolePreferences())}>
            Use default role preferences
          </Button>
        </div>
      )}

      {preferences.map((preference, index) => (
        <section
          key={`${index}-${preference.purpose}`}
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
            <h3 style={{ margin: 0 }}>{profileLabel(preference.purpose)} <span className="meta">· Role {index + 1}</span></h3>
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
                  <option key={value} value={value}>{profileLabel(value)}</option>
                ))}
              </select>
            </label>
          </div>

          <RouteFields
            label="Preferred route"
            route={preference.preferred_route}
            update={change => updateRoute(index, undefined, change)}
          />

          <div style={{ marginTop: '16px' }}>
            <h4 style={{ margin: '0 0 8px' }}>Explicit fallback routes</h4>
            {preference.fallback_routes.map((route, fallbackIndex) => (
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
                  route={route}
                  update={change => updateRoute(index, fallbackIndex, change)}
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
              defaultPreference('routine_implementation'),
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

      <div className="form-actions">
        {error && <p role="alert" className="field-error">{error}</p>}
        <Button type="submit" variant="primary" disabled={saving}>
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

export type { Profile, ProfileAppend, ProfileCreate, Preference, Route };
