'use client';
import { useState } from 'react';
import Link from 'next/link';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { RunList } from '@/components/runs/run-list';

const states: components['schemas']['RunState'][] = ['CREATED', 'PLANNING', 'AWAITING_PLAN_APPROVAL', 'PREPARING_WORKTREE', 'IMPLEMENTING', 'VALIDATING', 'REVIEWING', 'REMEDIATING', 'AWAITING_PR_APPROVAL', 'PUBLISHING_PR', 'MONITORING_PR', 'AWAITING_HUMAN_INTERVENTION', 'AWAITING_MERGE_APPROVAL', 'MERGING', 'PAUSED', 'COMPLETED', 'FAILED', 'CANCELLED'];
export default function RunsPage() {
  const [filters, setFilters] = useState({ state: '', project: '', attention: '', updated: '' });
  const [offset, setOffset] = useState(0);
  const [query, setQuery] = useState('');
  const projects = useApi<components['schemas']['ProjectResponse'][]>('/projects');
  const runs = useApi<components['schemas']['RunListPage']>(`/run-projections?offset=${offset}&limit=25${query}`);
  return <><h1>Runs</h1><p><Link href="/runs/new">New run</Link></p>
    <form className="form-grid" onSubmit={event => {
      event.preventDefault(); const params = new URLSearchParams();
      if (filters.state) params.set('state', filters.state);
      if (filters.project) params.set('project_id', filters.project);
      if (filters.attention) params.set('attention', filters.attention);
      if (filters.updated) params.set('updated_since', new Date(filters.updated).toISOString());
      setOffset(0); setQuery(params.size ? `&${params}` : '');
    }}>
      <label>State<select value={filters.state} onChange={event => setFilters({ ...filters, state: event.target.value })}>
        <option value="">All states</option>{states.map(state => <option key={state} value={state}>{state.replaceAll('_', ' ')}</option>)}</select></label>
      <label>Project<select value={filters.project} onChange={event => setFilters({ ...filters, project: event.target.value })}>
        <option value="">All projects</option>{projects.value?.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
      <label>Attention<select value={filters.attention} onChange={event => setFilters({ ...filters, attention: event.target.value })}>
        <option value="">All runs</option><option value="true">Needs attention</option><option value="false">No attention required</option></select></label>
      <label>Updated since (local time)<input type="datetime-local" value={filters.updated} onChange={event => setFilters({ ...filters, updated: event.target.value })} /></label>
      <button type="submit">Apply filters</button>
    </form>
    {projects.failed && <p role="alert">Project filters unavailable. <button onClick={projects.refresh}>Retry projects</button></p>}
    <button disabled={runs.loading} onClick={runs.refresh}>Refresh runs</button>
    {runs.loading && <p role="status">Loading runs…</p>}
    {runs.failed && <p role="alert">Runs unavailable. <button onClick={runs.refresh}>Retry</button></p>}
    {runs.value && <RunList items={runs.value.items} />}
    <nav aria-label="Run pages"><button disabled={!offset || runs.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</button>
      <button disabled={!runs.value?.truncated || runs.loading} onClick={() => setOffset(offset + 25)}>Next</button></nav>
  </>;
}
