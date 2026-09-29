'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { NewRunForm } from '@/components/runs/new-run-form';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { Button } from '@/components/ui/button';

export default function NewRunPage() {
  const router = useRouter();
  const projects = useApi<components['schemas']['ProjectResponse'][]>('/projects');
  return (
    <div className="new-run-page">
      <header className="page-header">
        <h1>New run</h1>
        <p className="page-description">Create a task for Forge to plan and execute within the selected project policy.</p>
      </header>
      {projects.failed && (
        <p role="alert">
          Projects could not be loaded.{' '}
          <Button onClick={projects.refresh}>Retry</Button>
        </p>
      )}
      {projects.loading && <p role="status">Loading projects…</p>}
      {projects.value && (
        <NewRunForm
          projects={projects.value}
          onCreated={id => router.push(`/runs/${encodeURIComponent(id)}`)}
        />
      )}
      {projects.value?.length === 0 && (
        <p>
          <Link href="/projects/new">Register project</Link>
        </p>
      )}
    </div>
  );
}
