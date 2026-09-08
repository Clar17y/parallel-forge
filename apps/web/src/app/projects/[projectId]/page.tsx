'use client';
import { useRef } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { useApi } from '@/hooks/use-api';
import { mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';
import { PolicyEditor } from '@/components/projects/policy-editor';

export default function ProjectPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const project = useApi<components['schemas']['ProjectResponse']>(`/projects/${projectId}`);
  const attempt = useRef<{ body: string; key: string } | null>(null);
  const value = project.value;
  const policy = useApi<components['schemas']['ProjectPolicyResponse']>(value?.policy_version ? `/projects/${projectId}/policy-versions/${value.policy_version}` : null);
  return <><h1>{value?.name ?? 'Project'}</h1>
    {project.loading && <p role="status">Loading project…</p>}
    {project.failed && <p role="alert">Project unavailable. <button onClick={project.refresh}>Retry</button></p>}
    {value && <>
      <p>{value.repository_path} · {value.github_repository} · {value.default_branch}</p>
      <p><Link href="/runs/new">New run</Link> · <Link href="/policies">Policy history</Link></p>
      {policy.loading && <p role="status">Loading policy…</p>}
      {policy.failed && <p role="alert">Policy unavailable. <button onClick={policy.refresh}>Retry</button></p>}
      {policy.value && <PolicyEditor policy={policy.value} onSave={async request => {
        const body = JSON.stringify({ projectId, request });
        if (attempt.current?.body !== body) attempt.current = { body, key: crypto.randomUUID() };
        const saved = await mutate<components['schemas']['ProjectPolicyResponse']>(`/projects/${projectId}/policy-versions`, request,
          { idempotencyKey: attempt.current.key });
        if (!saved) throw new Error('Policy response unavailable');
        project.refresh();
      }} />}
      <button onClick={project.refresh}>Reload current policy</button>
    </>}
  </>;
}
