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
      <p className="meta">
        Step 1 of getting started: tell Forge which repository to work in and what it may run. Defaults are pre-filled and advanced sections stay collapsed, so a name, a repository path, and one named command are enough to begin. Afterwards you will choose how agents run and start your first run — the project page tracks those steps.
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
