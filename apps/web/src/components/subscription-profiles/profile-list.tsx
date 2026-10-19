'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import type { components } from '@/lib/api/schema';
import { ApiError, api } from '@/lib/api/client';
import { stringifyExactJson } from '@/lib/api/exact-integer';
import { ProfileEditor } from './profile-editor';
import { profileLabel, deriveProfileSummaryLabel } from './labels';
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
  const latestProfiles = useMemo(() => {
    return orderedProfiles.filter(
      (profile, index) => index === 0 || orderedProfiles[index - 1].profile_id !== profile.profile_id
    );
  }, [orderedProfiles]);
  const historicalProfiles = useMemo(() => {
    return orderedProfiles.filter(
      (profile, index) => !(index === 0 || orderedProfiles[index - 1].profile_id !== profile.profile_id)
    );
  }, [orderedProfiles]);

  const [editorOpen, setEditorOpen] = useState(false);

  useEffect(() => {
    function checkHash() {
      if (typeof window !== 'undefined' && window.location.hash === '#profile-editor') {
        setEditorOpen(true);
      }
    }
    checkHash();
    window.addEventListener('hashchange', checkHash);
    return () => window.removeEventListener('hashchange', checkHash);
  }, []);

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
    const requestBody = stringifyExactJson(body);
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
        body: requestBody,
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

  function renderProfileCard(profile: Profile, latest: boolean) {
    return (
      <article
        key={`${profile.profile_id}:${profile.version}`}
        className="profile-version-card"
        data-latest={latest}
      >
        <div className="profile-card-header">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h3 style={{ margin: 0 }}>
                <span>{deriveProfileSummaryLabel(profile.preferences)}</span>
                <span className="meta ml-2 font-normal text-sm">
                  Profile {profile.profile_id.slice(0, 8)} · version {profile.version}
                </span>
              </h3>
            </div>
            <p className="meta" style={{ margin: '4px 0 0' }}>
              Default billing: <strong>{profileLabel(profile.default_billing_mode)}</strong>
              {' · '}
              Jev default:{' '}
              <strong>
                {profile.jev ? `${profileLabel(profile.jev.mode)} (${profile.jev.model})` : 'None'}
              </strong>
              {profile.jev?.allow_remote ? ' · remote processing allowed' : ''}
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

        <details>
          <summary>{latest ? 'Assigned roles' : 'Show historical roles'} <span className="meta">({profile.preferences.length})</span></summary>
          <div className="profile-roles-grid">
            {profile.preferences.map((preference, index) => {
              const preferred = (preference.preferred_route ?? {}) as Record<string, unknown>;
              const provider = String(preferred.provider ?? '');
              const cue = getProviderCue(provider);
              const fallbacks = ((preference.fallback_routes as Record<string, unknown>[]) ?? []);
              const friendlyPurpose = profileLabel(preference.purpose);

              return (
                <details
                  key={index}
                  className="disclosure-card card-provider"
                  data-provider={cue.tone}
                >
                  <summary className="disclosure-summary">
                    <div className="summary-line">
                      <div className="summary-title">
                        <span>{friendlyPurpose}</span>
                        <ProviderBadge provider={provider} />
                        <code>{String(preferred.model ?? '')}</code>
                      </div>
                      <div className="summary-meta">
                        <span>Reasoning: {profileLabel(preferred.effort)}</span>
                        {fallbacks.length > 0 && <span>{fallbacks.length} {fallbacks.length === 1 ? 'fallback' : 'fallbacks'}</span>}
                      </div>
                    </div>
                  </summary>
                  <div className="disclosure-body">
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
                    </div>
                    <div>
                      <span className="meta">Fallbacks: </span>
                      {fallbacks.length > 0 ? fallbacks.map(route => String(route.model)).join(', ') : 'none'}
                    </div>
                  </div>
                </details>
              );
            })}
          </div>
        </details>

        {profile.approved_mappings.length > 0 && <details>
          <summary>Approved mappings ({profile.approved_mappings.length})</summary>
          <ul>{profile.approved_mappings.map((mapping, index) => <li key={index}>
            {String(mapping.requested_model)} → {String(mapping.effective_model)} ({String(mapping.reason)})
          </li>)}</ul>
        </details>}

        <details>
          <summary>View immutable configuration</summary>
          <pre className="evidence-scroll">{JSON.stringify(profile, null, 2)}</pre>
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

      {!profiles.length && (
        <p className="empty-state">No subscription profiles yet. Create the first version below.</p>
      )}

      {latestProfiles.length > 0 && (
        <section aria-label="Latest subscription profiles" className="latest-profiles-section mb-6">
          <div className="space-y-4">
            {latestProfiles.map(profile => renderProfileCard(profile, true))}
          </div>
        </section>
      )}

      {profiles.length > 0 && (
        <details className="saved-profile-history mb-6">
          <summary>Saved profile history ({profiles.length})</summary>
          <section aria-label="Immutable profile versions" className="space-y-4 mt-3">
            <h2>Immutable profile versions</h2>
            <p className="meta">
              Each profile version is permanently recorded and immutable once created.
            </p>
            {historicalProfiles.map(profile => renderProfileCard(profile, false))}
          </section>
        </details>
      )}

      <details
        id="profile-editor"
        className="disclosure-card profile-editor-disclosure"
        open={Boolean(selected || editorOpen || profiles.length === 0)}
        onToggle={event => {
          setEditorOpen((event.currentTarget as HTMLDetailsElement).open);
        }}
      >
        <summary className="disclosure-summary cursor-pointer">
          <div className="summary-line">
            <span className="summary-title">
              {selected ? `Append profile version ${selected.version + 1}` : 'Create profile'}
            </span>
            <span className="summary-meta">
              {selected
                ? `Base: ${selected.profile_id} (version ${selected.version})`
                : 'Configure 9 runtime agent roles'}
            </span>
          </div>
        </summary>
        <div className="disclosure-body">
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
        </div>
      </details>
    </div>
  );
}

export { keyFor };
