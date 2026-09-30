'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import type { components } from '@/lib/api/schema';
import { ApiError, api } from '@/lib/api/client';
import { ProfileEditor } from './profile-editor';
import { profileLabel } from './labels';
import { Button } from '@/components/ui/button';
import { StatusBadge } from '@/components/ui/status-badge';
import { ProviderBadge, getProviderCue } from '@/components/ui/provider-badge';

type Profile = components['schemas']['ProfileResponse'];
type Create = components['schemas']['ProfileCreateRequest'];
type Append = components['schemas']['ProfileAppendRequest'];

function keyFor(body: unknown) {
  return `subscription-profile:${JSON.stringify(body)}`;
}

export function ProfileList({ profiles, refresh, unverified = false }: { profiles: Profile[]; refresh: () => void; unverified?: boolean }) {
  const [selected, setSelected] = useState<Profile>();
  const [newFormVersion, setNewFormVersion] = useState(0);
  const [stale, setStale] = useState(false);
  const [saving, setSaving] = useState(false);
  const attempts = useRef(new Map<string, string>());
  const orderedProfiles = useMemo(
    () => [...profiles].sort((a, b) => a.profile_id.localeCompare(b.profile_id) || b.version - a.version),
    [profiles]
  );
  const projectionKey = useMemo(
    () => profiles.map(profile => `${profile.profile_id}:${profile.version}`).join(','),
    [profiles]
  );
  const previousProjection = useRef(projectionKey);

  useEffect(() => {
    if (previousProjection.current !== projectionKey) {
      previousProjection.current = projectionKey;
      setStale(false);
    }
  }, [projectionKey]);

  async function save(body: Create | Append) {
    const path =
      'expected_current_version' in body
        ? `/subscription-profiles/${selected?.profile_id}/versions`
        : '/subscription-profiles';
    const bodyKey = keyFor({ path, body });
    const idempotencyKey = attempts.current.get(bodyKey) ?? crypto.randomUUID();
    attempts.current.set(bodyKey, idempotencyKey);
    setSaving(true);
    try {
      const result = await api<Profile>(path, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': idempotencyKey,
        },
        body: JSON.stringify(body),
      });
      attempts.current.delete(bodyKey);
      if (!('expected_current_version' in body)) setNewFormVersion(version => version + 1);
      setSelected(undefined);
      refresh();
      return result;
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) setStale(true);
      throw cause;
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="profiles-layout">
      {stale && (
        <p role="alert" className="field-error">
          This profile changed in another tab. Reload the profile history before editing again.{' '}
          <Button
            onClick={() => {
              setSelected(undefined);
              refresh();
            }}
          >
            Reload profile history
          </Button>
        </p>
      )}

      <section aria-label="Immutable profile versions">
        <h2>Immutable profile versions</h2>
        <p className="meta">
          Each profile version is permanently recorded and immutable once created.
        </p>

        {!profiles.length && (
          <p className="empty-state">No subscription profiles yet. Create the first version below.</p>
        )}

        {orderedProfiles.map((profile, index) => {
          const latest = index === 0 || orderedProfiles[index - 1].profile_id !== profile.profile_id;

          return (
            <article
              key={`${profile.profile_id}:${profile.version}`}
              className="profile-version-card"
              data-latest={latest}
            >
              <div className="profile-card-header">
                <div>
                  <h3>
                    Profile {profile.profile_id} · version {profile.version}
                  </h3>
                  <p className="meta" style={{ margin: '2px 0 0' }}>
                    Default billing: <strong>{profileLabel(profile.default_billing_mode)}</strong>
                    {' · '}
                    Jev default:{' '}
                    <strong>
                      {profile.jev ? `${profileLabel(profile.jev.mode)} (${profile.jev.model})` : 'None'}
                    </strong>
                  </p>
                </div>
                <div>
                  {latest ? (
                    <StatusBadge label="Latest version" tone="success" />
                  ) : (
                    <StatusBadge label="Historical version" tone="neutral" />
                  )}
                </div>
              </div>

              <details open={latest}>
                <summary>{latest ? 'Assigned roles' : 'Show historical roles'}</summary>
              <div className="profile-roles-grid">
                {profile.preferences.map((preference, index) => {
                  const preferred = (preference.preferred_route ?? {}) as Record<string, unknown>;
                  const provider = String(preferred.provider ?? '');
                  const cue = getProviderCue(provider);
                  const fallbacks = ((preference.fallback_routes as Record<string, unknown>[]) ?? []);
                  const friendlyPurpose = profileLabel(preference.purpose);

                  return (
                    <div
                      key={index}
                      className="profile-role-card card-provider"
                      data-provider={cue.tone}
                    >
                      <div className="profile-role-card-header">
                        <div>
                          <span className="profile-role-title">{friendlyPurpose}</span>
                        </div>
                        <ProviderBadge provider={provider} />
                      </div>

                      <div style={{ fontSize: '0.8125rem', display: 'grid', gap: '4px' }}>
                        <div>
                          <span className="meta">Requested model: </span>
                          <strong style={{ fontFamily: 'monospace' }}>
                            {String(preferred.model ?? '')}
                          </strong>
                        </div>
                        <div>
                          <span className="meta">Client: </span>
                          <span>{profileLabel(preferred.client)}</span>
                        </div>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                          <span className="meta">
                            Auth: <strong>{profileLabel(preferred.auth_mode)}</strong>
                          </span>
                          <span className="meta">
                            Billing: <strong>{profileLabel(preferred.billing_mode)}</strong>
                          </span>
                          <span className="meta">
                            Effort: <strong>{profileLabel(preferred.effort)}</strong>
                          </span>
                        </div>
                      </div>

                      <div style={{ borderTop: '1px solid var(--border)', paddingTop: '6px', fontSize: '0.75rem' }}>
                        <span className="meta">Fallbacks: </span>
                        {fallbacks.length > 0 ? (
                          <span style={{ fontWeight: 500 }}>
                            {fallbacks.map(route => String(route.model)).join(', ')}
                          </span>
                        ) : (
                          <span>none</span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
              </details>

              <p style={{ margin: '4px 0 0', fontSize: '0.8125rem' }}>
                Approved mappings:{' '}
                {profile.approved_mappings
                  .map(
                    mapping =>
                      `${String(mapping.requested_model)} → ${String(mapping.effective_model)} (${String(
                        mapping.reason
                      )})`
                  )
                  .join('; ') || 'none'}
              </p>

              <p style={{ margin: '4px 0 0', fontSize: '0.8125rem' }}>
                Jev default:{' '}
                {profile.jev
                  ? `${profileLabel(profile.jev.mode)} · ${profile.jev.model}${
                      profile.jev.allow_remote ? ' · remote processing allowed' : ''
                    }`
                  : 'None'}
              </p>

              <details>
                <summary>View immutable configuration</summary>
                <pre>{JSON.stringify(profile, null, 2)}</pre>
              </details>

              {latest && !stale && (
                <div style={{ marginTop: '4px' }}>
                  <Button type="button" disabled={saving || unverified} onClick={() => setSelected(profile)}>
                    Append from latest version {profile.version}
                  </Button>
                </div>
              )}
            </article>
          );
        })}
      </section>

      <section aria-label="Profile editor container">
        {selected && (
          <div className="append-notice">
            <span>
              Appending version <strong>{selected.version + 1}</strong> based on Profile{' '}
              <code>{selected.profile_id}</code> version {selected.version}.
            </span>
            <Button variant="quiet" disabled={saving} onClick={() => setSelected(undefined)}>
              Cancel editing
            </Button>
          </div>
        )}

        <h2>{selected ? `Append profile version ${selected.version + 1}` : 'Create profile'}</h2>

        <ProfileEditor
          key={selected ? `${selected.profile_id}:${selected.version}` : `new:${newFormVersion}`}
          initial={selected}
          expectedVersion={selected?.version}
          onSave={save}
          saveUnavailable={unverified || stale}
        />
      </section>
    </div>
  );
}

export { keyFor };
