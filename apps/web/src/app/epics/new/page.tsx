'use client';

import { Suspense } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { useApi } from '@/hooks/use-api';
import { Button } from '@/components/ui/button';
import { EpicCreateForm } from '@/components/epics/epic-create-form';
import type { components } from '@/lib/api/schema';

function NewEpicContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const defaultProjectId = searchParams.get('project_id') ?? undefined;

  const projectsApi = useApi<components['schemas']['ProjectResponse'][]>('/projects');
  const projects = projectsApi.value ?? [];

  return (
    <div className="new-epic-page space-y-6">
      <header className="border-b border-[var(--border)] pb-4">
        <h1 className="text-2xl font-bold">Create New Epic</h1>
        <p className="text-sm text-[var(--muted)]">
          Start a project epic with a requirements brief and work-item backlog.
        </p>
      </header>

      {projectsApi.loading && <p role="status">Loading projects…</p>}

      {projectsApi.failed && (
        <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)]">
          <p>Projects could not be loaded.</p>
          <Button variant="secondary" onClick={projectsApi.refresh} className="mt-2">
            Retry
          </Button>
        </div>
      )}

      <EpicCreateForm
          projects={projects}
          defaultProjectId={defaultProjectId}
          onCreated={epicId => router.push(`/epics/${encodeURIComponent(epicId)}`)}
        />

      {projectsApi.value && projects.length === 0 && (
        <p className="text-sm text-[var(--muted)]">
          No projects found.{' '}
          <Link href="/projects/new" className="text-[var(--focus)] hover:underline">
            Register a project first.
          </Link>
        </p>
      )}
    </div>
  );
}

export default function NewEpicPage() {
  return (
    <Suspense fallback={<p role="status">Loading new epic form…</p>}>
      <NewEpicContent />
    </Suspense>
  );
}
