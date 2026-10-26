'use client';
import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { LoadingStatus } from '@/components/ui/loading-status';
import { useApi } from '@/hooks/use-api';
import { mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';

type Run = components['schemas']['DirectRunResponse'];
type Project = components['schemas']['ProjectResponse'];
type Route = components['schemas']['DirectRouteResponse'];

export default function DirectPage() {
  const router = useRouter();
  const runs = useApi<Run[]>('/direct-runs');
  const projects = useApi<Project[]>('/projects');
  const routes = useApi<Route[]>('/direct-runs/routes');
  const [projectId, setProjectId] = useState('');
  const [task, setTask] = useState('');
  const [shape, setShape] = useState<'quick' | 'planned'>('quick');
  const [reviewPlanFirst, setReviewPlanFirst] = useState(false);
  const [routeKey, setRouteKey] = useState('');
  const [error, setError] = useState('');
  const routeList = routes.value ?? [];
  const selected = routeList.find((r, i) => `${i}` === routeKey) ?? routeList[0];

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError('');
    const project = projectId || projects.value?.[0]?.id;
    if (!project || !selected || !task.trim()) { setError('Project, task and route are required.'); return; }
    try {
      const created = await mutate<Run>('/direct-runs', {
        project_id: project, task, client: selected.client, model: selected.model,
        effort: selected.effort ?? undefined, shape,
        review_plan_first: shape === 'planned' && reviewPlanFirst,
      }, { idempotencyKey: crypto.randomUUID() });
      if (created) router.push(`/direct/${encodeURIComponent(created.id)}`);
    } catch { setError('Direct run could not be created.'); }
  }

  return <>
    <div className="page-title-row"><h1>Direct runs</h1></div>
    <form onSubmit={submit}>
      <label>Project <select aria-label="Project" value={projectId} onChange={e => setProjectId(e.target.value)}>
        {(projects.value ?? []).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select></label>
      <label>Task <textarea aria-label="Task" value={task} onChange={e => setTask(e.target.value)} /></label>
      <label>Shape <select aria-label="Shape" value={shape} onChange={e => setShape(e.target.value as 'quick' | 'planned')}>
        <option value="quick">Quick implementation</option>
        <option value="planned">Planned (plan session first)</option>
      </select></label>
      {shape === 'planned' && <label><input type="checkbox" checked={reviewPlanFirst} onChange={e => setReviewPlanFirst(e.target.checked)} /> Review plan before implementation</label>}
      <label>Route <select aria-label="Route" value={routeKey} onChange={e => setRouteKey(e.target.value)}>
        {routeList.map((r, i) => <option key={`${r.client}-${r.model}-${r.effort}`} value={`${i}`}>{`${r.client} / ${r.model}${r.effort ? ` / ${r.effort}` : ''}`}</option>)}
      </select></label>
      {error && <p role="alert">{error}</p>}
      <button type="submit">Start direct run</button>
    </form>
    {runs.loading && <LoadingStatus>Loading direct runs…</LoadingStatus>}
    {runs.failed && <p role="alert">Direct runs unavailable.</p>}
    {runs.value && <ul>{runs.value.map(run => (
      <li key={run.id}><Link href={`/direct/${encodeURIComponent(run.id)}`}>{run.state} — {run.task.slice(0, 80)}</Link></li>
    ))}</ul>}
  </>;
}
