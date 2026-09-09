'use client';
import { useRef } from 'react';
import { useRouter } from 'next/navigation';
import { ProjectForm } from '@/components/projects/project-form';
import { mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';

export default function NewProjectPage() {
  const router = useRouter();
  const attempt = useRef<{ body: string; key: string } | null>(null);
  return <><h1>Register project</h1><ProjectForm onSave={async request => {
    const body = JSON.stringify(request);
    if (attempt.current?.body !== body) attempt.current = { body, key: crypto.randomUUID() };
    const project = await mutate<components['schemas']['ProjectResponse']>('/projects', request,
      { idempotencyKey: attempt.current.key });
    if (!project?.id) throw new Error('Project response unavailable');
    router.push(`/projects/${encodeURIComponent(project.id)}`);
  }} /></>;
}
