'use client';

import { useRef, useState } from 'react';
import Link from 'next/link';
import type { components } from '@/lib/api/schema';
import { ApiError, api } from '@/lib/api/client';
import { Button } from '@/components/ui/button';
import { profileLabel } from './labels';
import type { Preference, Route } from './models';

type Profile = components['schemas']['ProfileResponse'];

export function ProfileSelector({
  projectId,
  profiles,
  current,
  refresh,
}: {
  projectId: string;
  profiles: Profile[];
  current: Profile | null | undefined;
  refresh: () => void;
}) {
  const [profileId, setProfileId] = useState(current?.profile_id ?? '');
  const [version, setVersion] = useState(String(current?.version ?? ''));
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);
  const [stale, setStale] = useState(false);
  const attempt = useRef<{ body: string; key: string } | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(undefined);
    if (stale) return;
    const profile = profiles.find(
      value => value.profile_id === profileId && String(value.version) === version
    );
    if (!profile) {
      setError('Choose an available profile version.');
      return;
    }
    const request: components['schemas']['ProjectProfileSelectRequest'] = {
      profile_id: profile.profile_id,
      profile_version: profile.version,
      ...(current
        ? {
            expected_profile_id: current.profile_id,
            expected_profile_version: current.version,
          }
        : {}),
    };
    const body = JSON.stringify(request);
    if (attempt.current?.body !== body) {
      attempt.current = { body, key: crypto.randomUUID() };
    }
    setSaving(true);
    try {
      await api(`/projects/${projectId}/subscription-profile`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': attempt.current.key,
        },
        body,
      });
      refresh();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        setStale(true);
        setError('The project selection changed in another tab. Reload before selecting again.');
      } else {
        setError('Project profile selection failed.');
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="panel" aria-label="Project subscription profile">
      <h2>Project subscription profile</h2>
      <p className="meta">
        Selection is versioned and remains separate from the{' '}
        <a href="/policies" className="underline">project safety policy</a>.
      </p>

      <form onSubmit={submit}>
        <div className="form-field" style={{ maxWidth: '400px' }}>
          <label>
            Profile version
            <select
              aria-label="Profile version"
              disabled={stale}
              value={`${profileId}:${version}`}
              onChange={event => {
                const [id, selectedVersion] = event.target.value.split(':');
                setProfileId(id);
                setVersion(selectedVersion);
              }}
            >
              <option value=":">Choose a profile version</option>
              {profiles.map(profile => (
                <option
                  key={`${profile.profile_id}:${profile.version}`}
                  value={`${profile.profile_id}:${profile.version}`}
                >
                  Version {profile.version} · {profile.profile_id}
                  {profile.jev ? ` (Jev: ${profileLabel(profile.jev.mode)})` : ''}
                </option>
              ))}
            </select>
          </label>
        </div>

        {current && (
          <div className="meta" style={{ marginTop: '8px' }}>
            <p style={{ margin: 0 }}>
              Current selection: <strong>version {current.version}</strong> · <code>{current.profile_id}</code>
            </p>
            {current.preferences && current.preferences.length > 0 && (
              <div style={{ marginTop: '4px' }}>
                <p style={{ margin: 0 }}>
                  <strong>Role models &amp; reasoning:</strong>
                </p>
                <ul style={{ margin: '4px 0 0', paddingLeft: '20px' }}>
                  {(current.preferences as unknown as Preference[]).map((pref, idx) => {
                    const purpose = String(pref.purpose ?? `Role ${idx + 1}`);
                    const route = pref.preferred_route as Route | undefined;
                    return (
                      <li key={purpose}>
                        <strong>{profileLabel(purpose)}</strong>:{' '}
                        <code>{route?.model || 'Unset'}</code>
                        {route?.effort ? ` (Reasoning: ${route.effort})` : ''}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}
            <p style={{ margin: '4px 0 0' }}>
              Jev default:{' '}
              <strong>
                {current.jev ? `${profileLabel(current.jev.mode)} (${current.jev.model})` : 'None'}
              </strong>
            </p>
            <p style={{ margin: '8px 0 0' }}>
              <Link href="/subscription-profiles" className="underline">
                Edit subscription profiles
              </Link>
            </p>
          </div>
        )}
        {!current && (
          <p className="meta" style={{ marginTop: '8px' }}>
            <Link href="/subscription-profiles" className="underline">
              Edit subscription profiles
            </Link>
          </p>
        )}

        {error && <p role="alert" className="field-error">{error}</p>}

        <div className="form-actions" style={{ marginTop: '16px' }}>
          {stale && (
            <Button
              type="button"
              variant="secondary"
              onClick={() => {
                setError(undefined);
                refresh();
              }}
            >
              Reload project selection
            </Button>
          )}
          <Button type="submit" variant="primary" disabled={saving || stale}>
            {saving ? 'Saving…' : 'Select profile version'}
          </Button>
        </div>
      </form>
    </section>
  );
}
