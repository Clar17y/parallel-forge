'use client';
import { useState, type ReactNode } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

export function PolicyBrowser({ children }: {
  children: (policy: components['schemas']['ProjectPolicyResponse'], project: components['schemas']['ProjectResponse']) => ReactNode;
}) {
  const projects = useApi<components['schemas']['ProjectResponse'][]>('/projects');
  const [selected, setSelected] = useState('');
  const [version, setVersion] = useState('');
  const project = projects.value?.find(item => item.id === selected) ?? projects.value?.[0];
  const requested = version || String(project?.policy_version ?? '');
  const policy = useApi<components['schemas']['ProjectPolicyResponse']>(project && /^[1-9]\d*$/.test(requested) ? `/projects/${project.id}/policy-versions/${requested}` : null);
  return <>
    {projects.loading && <p role="status">Loading projects…</p>}
    {projects.failed && <p role="alert">Projects unavailable. <button onClick={projects.refresh}>Retry</button></p>}
    {projects.value && !projects.value.length && <p>No project policies yet.</p>}
    {project && <>
      <label>Project <select value={project.id} onChange={event => { setSelected(event.target.value); setVersion(''); }}>
        {projects.value?.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label>
      <label>Policy version <input type="number" min={1} max={project.policy_version ?? undefined} value={requested} onChange={event => setVersion(event.target.value)} /></label>
      {policy.loading && <p role="status">Loading policy…</p>}
      {policy.failed && <p role="alert">Policy unavailable. <button onClick={policy.refresh}>Retry</button></p>}
      {policy.value && children(policy.value, project)}
    </>}
  </>;
}
