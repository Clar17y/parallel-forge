'use client';

import { useRef, useState } from 'react';
import type { components } from '@/lib/api/schema';
import { ApiError, api } from '@/lib/api/client';

type Profile = components['schemas']['ProfileResponse'];
export function ProfileSelector({ projectId, profiles, current, refresh }: { projectId: string; profiles: Profile[]; current: Profile | null | undefined; refresh: () => void }) {
  const [profileId, setProfileId] = useState(current?.profile_id ?? '');
  const [version, setVersion] = useState(String(current?.version ?? ''));
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);
  const [stale, setStale] = useState(false);
  const attempt = useRef<{ body: string; key: string } | null>(null);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setError(undefined); if (stale) return;
    const profile = profiles.find(value => value.profile_id === profileId && String(value.version) === version);
    if (!profile) { setError('Choose an available profile version.'); return; }
    const request: components['schemas']['ProjectProfileSelectRequest'] = { profile_id: profile.profile_id, profile_version: profile.version, ...(current ? { expected_profile_id: current.profile_id, expected_profile_version: current.version } : {}) };
    const body = JSON.stringify(request);
    if (attempt.current?.body !== body) attempt.current = { body, key: crypto.randomUUID() };
    setSaving(true);
    try { await api(`/projects/${projectId}/subscription-profile`, { method: 'PUT', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': attempt.current.key }, body }); refresh(); }
    catch (cause) { if (cause instanceof ApiError && cause.status === 409) { setStale(true); setError('The project selection changed in another tab. Reload before selecting again.'); } else setError('Project profile selection failed.'); }
    finally { setSaving(false); }
  }
  return <section><h2>Project subscription profile</h2><p>Selection is versioned and remains separate from the project safety policy.</p><form onSubmit={submit}><label>Profile version<select aria-label="Profile version" disabled={stale} value={`${profileId}:${version}`} onChange={event => { const [id, selectedVersion] = event.target.value.split(':'); setProfileId(id); setVersion(selectedVersion); }}><option value=":">Choose a profile version</option>{profiles.map(profile => <option key={`${profile.profile_id}:${profile.version}`} value={`${profile.profile_id}:${profile.version}`}>Version {profile.version} · {profile.profile_id}</option>)}</select></label>{current && <p>Current selection: version {current.version} · {current.profile_id}</p>}{error && <p role="alert">{error}</p>}{stale && <button type="button" onClick={() => { setError(undefined); refresh(); }}>Reload project selection</button>}<button type="submit" disabled={saving || stale}>{saving ? 'Saving…' : 'Select profile version'}</button></form></section>;
}
