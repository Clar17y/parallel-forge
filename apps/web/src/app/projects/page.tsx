'use client';
import { LoadingStatus } from '@/components/ui/loading-status';
import Link from 'next/link';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { ProjectList } from '@/components/projects/project-list';

export default function ProjectsPage() {
  const projects = useApi<components['schemas']['ProjectResponse'][]>('/projects');
  return <><h1>Projects</h1><p><Link href="/projects/new">Register project</Link></p>
    {projects.loading && <LoadingStatus>Loading projects…</LoadingStatus>}
    {projects.failed && <p role="alert">Projects unavailable. <button onClick={projects.refresh}>Retry</button></p>}
    {projects.value && <ProjectList projects={projects.value} />}
  </>;
}
