'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { NewRunForm } from '@/components/runs/new-run-form';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

export default function NewRunPage() {
  const router = useRouter();
  const projects = useApi<components['schemas']['ProjectResponse'][]>('/projects');
  return <>
    <h1>New run</h1>
    <p>Create a task for Forge to plan and execute within the selected project policy.</p>
    {projects.failed && <p role="alert">Projects could not be loaded. <button onClick={projects.refresh}>Retry</button></p>}
    {projects.loading && <p role="status">Loading projects…</p>}
    {projects.value && <NewRunForm projects={projects.value} onCreated={id => router.push(`/runs/${encodeURIComponent(id)}`)} />}
    {projects.value?.length === 0 && <Link href="/projects/new">Register project</Link>}
  </>;
}
