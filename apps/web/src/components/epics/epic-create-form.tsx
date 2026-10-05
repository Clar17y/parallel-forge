'use client';

import { useEffect, useState, type FormEvent } from 'react';
import { Button } from '@/components/ui/button';
import { useEpicMutations } from '@/hooks/epics/use-epic-mutations';
import type { components } from '@/lib/api/schema';
import type { EpicResponse } from '@/hooks/epics/types';

export function EpicCreateForm({
  projects,
  defaultProjectId,
  onCreated,
}: {
  projects: components['schemas']['ProjectResponse'][];
  defaultProjectId?: string;
  onCreated: (epicId: string) => void;
}) {
  const [projectId, setProjectId] = useState(defaultProjectId ?? projects[0]?.id ?? '');
  const [title, setTitle] = useState('');
  const [problem, setProblem] = useState('');
  const [recoveredKey, setRecoveredKey] = useState<string | null>(null);

  const mutations = useEpicMutations('create');
  const { registerCompletion, pendingMutation: request } = mutations;
  useEffect(() => registerCompletion('create', value => onCreated((value as EpicResponse).epic_id)), [registerCompletion, onCreated]);
  if (request?.kind === 'create' && recoveredKey !== request.idempotencyKey) {
      setRecoveredKey(request.idempotencyKey);
      if (typeof request.body.title === 'string') setTitle(request.body.title);
      if (typeof request.body.project_id === 'string') setProjectId(request.body.project_id);
      const draft = request.body.draft as { problem?: string } | undefined;
      setProblem(draft?.problem ?? '');
  }
  const selectedProjectId = request?.kind === 'create' && typeof request.body.project_id === 'string'
    ? request.body.project_id
    : projects.some(project => project.id === projectId) ? projectId
      : projects.find(project => project.id === defaultProjectId)?.id ?? projects[0]?.id ?? projectId;

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim() || !selectedProjectId) return;

    try {
      const payload: Record<string, unknown> = {
        schema_version: 1,
        project_id: selectedProjectId,
        title: title.trim(),
      };
      if (problem.trim()) {
        payload.draft = {
          schema_version: 1,
          problem: problem.trim(),
          outcomes: [],
          scope: [],
          exclusions: [],
          requirements: [],
          decisions: [],
          assumptions: [],
          open_questions: [],
        };
      }

      await mutations.execute<EpicResponse>('POST', '/epics', payload, { kind: 'create' });
    } catch {
      // Mutations hook captures error
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4 max-w-xl">
      {mutations.hasPendingRetry && !mutations.loading && (
        <div role="alert" className="p-3 bg-[var(--warning-soft)] text-[var(--warning)] rounded text-sm space-y-2">
          <p className="font-semibold">Network error. Mutation outcome uncertain.</p>
          <Button type="button" variant="primary" disabled={mutations.loading} onClick={() => { void mutations.retryPending().catch(() => undefined); }}>
            {mutations.loading ? 'Retrying…' : 'Retry original request'}
          </Button>
        </div>
      )}
      {mutations.hasPendingRetry && !mutations.reloadProtected && <p role="alert">This browser cannot preserve the pending request if you reload or close this tab. You can navigate within this tab, but keep it open and retry the original request.</p>}

      {mutations.error && !mutations.hasPendingRetry && (
        <div role="alert" className="p-3 bg-[var(--danger-soft)] text-[var(--danger)] rounded text-sm">
          {mutations.error}
        </div>
      )}

      <fieldset disabled={mutations.loading || mutations.hasPendingRetry} className="space-y-4 min-w-0">
      <legend className="sr-only">New epic details</legend>
      <div>
        <label htmlFor="project-select" className="block text-sm font-medium mb-1">
          Project
        </label>
        <select
          id="project-select"
          className="w-full px-3 py-2 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
          value={selectedProjectId}
          onChange={e => setProjectId(e.target.value)}
          required
        >
          {selectedProjectId && !projects.some(project => project.id === selectedProjectId) && <option value={selectedProjectId}>Project selection unavailable</option>}
          {projects.map(p => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label htmlFor="epic-create-title" className="block text-sm font-medium mb-1">
          Epic Title
        </label>
        <input
          id="epic-create-title"
          type="text"
          className="w-full px-3 py-2 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
          value={title}
          onChange={e => setTitle(e.target.value)}
          placeholder="e.g. Distributed Task Queue & Checkpoints"
          maxLength={256}
          required
        />
      </div>

      <div>
        <label htmlFor="epic-create-problem" className="block text-sm font-medium mb-1">
          Initial Problem Statement (Optional)
        </label>
        <textarea
          id="epic-create-problem"
          className="w-full px-3 py-2 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)] min-h-[100px]"
          value={problem}
          onChange={e => setProblem(e.target.value)}
          placeholder="Brief description of the problem this epic addresses..."
          maxLength={10000}
        />
      </div>

      <div className="pt-2">
        <Button
          type="submit"
          variant="primary"
          disabled={mutations.loading || mutations.hasPendingRetry || !title.trim() || !selectedProjectId || !projects.length}
        >
          {mutations.loading ? 'Creating epic…' : 'Create Epic'}
        </Button>
      </div>
      </fieldset>
    </form>
  );
}
