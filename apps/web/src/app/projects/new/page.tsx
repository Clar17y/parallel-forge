'use client';
import { useRef } from 'react';
import { useRouter } from 'next/navigation';
import { ProjectForm } from '@/components/projects/project-form';
import { mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';

export default function NewProjectPage() {
  const router = useRouter();
  const attempt = useRef<{ body: string; key: string } | null>(null);
  return (
    <>
      <h1>Register project</h1>
      <p className="meta readable-prose mb-4">
        Tell Forge which repository to work in and what commands it may run. Defaults are pre-filled and advanced sections stay collapsed, so a name, repository path, GitHub repository (owner/repo), and one named command are enough to begin.
      </p>
      <ProjectForm onSave={async request => {
        const body = JSON.stringify(request);
        if (attempt.current?.body !== body) attempt.current = { body, key: crypto.randomUUID() };
        const project = await mutate<components['schemas']['ProjectResponse']>('/projects', request,
          { idempotencyKey: attempt.current.key });
        if (!project?.id) throw new Error('Project response unavailable');
        router.push(`/projects/${encodeURIComponent(project.id)}`);
      }} />
    </>
  );
}
