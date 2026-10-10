'use client';

import { LoadingStatus } from '@/components/ui/loading-status';
import { Suspense, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { useApi } from '@/hooks/use-api';
import { Button } from '@/components/ui/button';
import { EpicList } from '@/components/epics/epic-list';
import type { components } from '@/lib/api/schema';
import type { EpicResponse } from '@/hooks/epics/types';

function EpicsPageContent() {
  const searchParams = useSearchParams();
  const initialProject = searchParams.get('project_id') ?? '';

  const [selectedProject, setSelectedProject] = useState(initialProject);

  const projectsApi = useApi<components['schemas']['ProjectResponse'][]>('/projects');
  const projects = projectsApi.value ?? [];

  // Update selected project if projects load and none is selected
  const activeProjectId = selectedProject || (projects.length > 0 ? projects[0].id : '');

  const epicsPath = activeProjectId
    ? `/epics?project_id=${encodeURIComponent(activeProjectId)}`
    : '/epics';

  const epicsApi = useApi<EpicResponse[]>(activeProjectId ? epicsPath : null, {
    refreshIntervalMs: 5000,
    keepPreviousOnRefresh: true,
  });

  return (
    <div className="epics-page space-y-6">
      <header className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-[var(--border)] pb-4">
        <div>
          <h1 className="text-2xl font-bold">Project Epics & Requirements</h1>
          <p className="text-sm text-[var(--muted)]">
            Manage project requirements briefs, work-item decomposition graphs, and execution progress.
          </p>
        </div>
        <Link href={`/epics/new${activeProjectId ? `?project_id=${encodeURIComponent(activeProjectId)}` : ''}`} className="button" data-variant="primary">
          + New Epic
        </Link>
      </header>

      {/* Project selector */}
      <div className="flex flex-col sm:flex-row sm:items-center gap-3">
        <label htmlFor="project-filter" className="text-sm font-medium">
          Select Project:
        </label>
        <select
          id="project-filter"
          className="px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)] max-w-xs"
          value={activeProjectId}
          disabled={!projects.length}
          onChange={e => setSelectedProject(e.target.value)}
        >
          {projects.map(p => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <Button variant="quiet" onClick={() => epicsApi.refresh()} disabled={epicsApi.loading || !activeProjectId}>
          {epicsApi.loading ? 'Refreshing…' : 'Refresh'}
        </Button>
      </div>

      {projectsApi.loading && <LoadingStatus>Loading projects…</LoadingStatus>}
      {projectsApi.value && projects.length === 0 && <div className="space-y-2">
        <p>No projects are registered yet.</p>
        <Link href="/projects/new" className="text-[var(--focus)] hover:underline">Register a project</Link>
      </div>}

      {projectsApi.failed && (
        <p role="alert" className="text-sm text-[var(--danger)]">
          Failed to load registered projects.{' '}
          <Button variant="quiet" onClick={projectsApi.refresh}>Retry</Button>
        </p>
      )}

      {epicsApi.loading && !epicsApi.value && <LoadingStatus>Loading project epics…</LoadingStatus>}

      {epicsApi.failed && (
        <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)] space-y-2">
          <p>Failed to load epics for the selected project.</p>
          <Button variant="secondary" onClick={epicsApi.refresh}>Retry</Button>
        </div>
      )}

      {epicsApi.value && <EpicList epics={epicsApi.value} />}
    </div>
  );
}

export default function EpicsPage() {
  return (
    <Suspense fallback={<LoadingStatus>Loading epics workspace…</LoadingStatus>}>
      <EpicsPageContent />
    </Suspense>
  );
}
