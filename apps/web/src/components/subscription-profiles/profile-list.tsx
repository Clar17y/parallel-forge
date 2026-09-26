'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import type { components } from '@/lib/api/schema';
import { ApiError, api } from '@/lib/api/client';
import { ProfileEditor } from './profile-editor';

type Profile = components['schemas']['ProfileResponse'];
type Create = components['schemas']['ProfileCreateRequest'];
type Append = components['schemas']['ProfileAppendRequest'];

function keyFor(body: unknown) { return `subscription-profile:${JSON.stringify(body)}`; }

export function ProfileList({ profiles, refresh }: { profiles: Profile[]; refresh: () => void }) {
  const [selected, setSelected] = useState<Profile>();
  const [stale, setStale] = useState(false);
  const attempts = useRef(new Map<string, string>());
  const projectionKey = useMemo(() => profiles.map(profile => `${profile.profile_id}:${profile.version}`).join(','), [profiles]);
  const previousProjection = useRef(projectionKey);
  useEffect(() => { if (previousProjection.current !== projectionKey) { previousProjection.current = projectionKey; setStale(false); } }, [projectionKey]);
  async function save(body: Create | Append) {
    const path = 'expected_current_version' in body ? `/subscription-profiles/${selected?.profile_id}/versions` : '/subscription-profiles';
    const bodyKey = keyFor({ path, body });
    const idempotencyKey = attempts.current.get(bodyKey) ?? crypto.randomUUID();
    attempts.current.set(bodyKey, idempotencyKey);
    try {
      const result = await api<Profile>(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey }, body: JSON.stringify(body) });
      setSelected(undefined); refresh(); return result;
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) setStale(true);
      throw cause;
    }
  }
  return <section>
    {stale && <p role="alert">This profile changed in another tab. Reload the profile history before editing again. <button onClick={() => { setSelected(undefined); refresh(); }}>Reload profile history</button></p>}
    <h2>Immutable profile versions</h2>
    {!profiles.length && <p>No subscription profiles yet. Create the first version below.</p>}
    {profiles.map(profile => { const latest = profile.version === Math.max(...profiles.filter(item => item.profile_id === profile.profile_id).map(item => item.version)); return <article key={`${profile.profile_id}:${profile.version}`}><h3>Profile {profile.profile_id} · version {profile.version}</h3><p>Default billing: {profile.default_billing_mode}</p>{profile.preferences.map((preference, index) => <div key={index}><strong>{String(preference.purpose)}</strong>: requested {String((preference.preferred_route as Record<string, unknown>).model)} via {String((preference.preferred_route as Record<string, unknown>).client)}; auth {String((preference.preferred_route as Record<string, unknown>).auth_mode)}, billing {String((preference.preferred_route as Record<string, unknown>).billing_mode)}. Fallbacks: {((preference.fallback_routes as Record<string, unknown>[]) ?? []).map(route => String(route.model)).join(', ') || 'none'}</div>)}<p>Approved mappings: {profile.approved_mappings.map(mapping => `${String(mapping.requested_model)} → ${String(mapping.effective_model)} (${String(mapping.reason)})`).join('; ') || 'none'}</p><details><summary>View immutable configuration</summary><pre>{JSON.stringify(profile, null, 2)}</pre></details>{latest && !stale && <button type="button" onClick={() => setSelected(profile)}>Append from latest version {profile.version}</button>}</article>; })}
    <h2>{selected ? `Append profile version ${selected.version + 1}` : 'Create profile'}</h2>
    {!stale && <ProfileEditor key={selected ? `${selected.profile_id}:${selected.version}` : 'new'} initial={selected} expectedVersion={selected?.version} onSave={save} />}
  </section>;
}

export { keyFor };
