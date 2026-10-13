'use client';
import { LoadingStatus } from '@/components/ui/loading-status';
import { Suspense, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { RunList } from '@/components/runs/run-list';
import { RUN_BUCKETS, bucketById, bucketCounts } from '@/components/runs/run-buckets';

const UPDATED_OPTIONS = [
  { id: 'any', label: 'Any time', days: null },
  { id: '24h', label: 'Last 24 hours', days: 1 },
  { id: '7d', label: 'Last 7 days', days: 7 },
  { id: '30d', label: 'Last 30 days', days: 30 },
] as const;

function RunsPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const bucket = bucketById(searchParams.get('bucket'));
  const projectId = searchParams.get('project_id') ?? '';
  // The API caps q at 200 characters; slice here too so a hand-edited URL
  // cannot debounce (or load) into a 422.
  const queryText = (searchParams.get('q') ?? '').slice(0, 200);
  const attentionParam = searchParams.get('attention');
  const attention = attentionParam === 'true' ? true : attentionParam === 'false' ? false : null;
  const updated = UPDATED_OPTIONS.find(option => option.id === searchParams.get('updated')) ?? UPDATED_OPTIONS[0];
  const offsetParam = Number(searchParams.get('offset') ?? '0');
  const offset = Number.isSafeInteger(offsetParam) && offsetParam > 0 ? offsetParam : 0;

  const update = (patch: Record<string, string | null>) => {
    const params = new URLSearchParams(searchParams.toString());
    for (const [key, value] of Object.entries(patch)) {
      if (value) params.set(key, value);
      else params.delete(key);
    }
    const qs = params.toString();
    router.replace(qs ? `/runs?${qs}` : '/runs', { scroll: false });
  };

  const [searchInput, setSearchInput] = useState(queryText);
  // Keep the input following the URL (back/forward navigation) without an
  // effect: adjust state during render when the param changes externally.
  const [syncedQuery, setSyncedQuery] = useState(queryText);
  if (queryText !== syncedQuery) {
    setSyncedQuery(queryText);
    setSearchInput(queryText);
  }
  useEffect(() => {
    if (searchInput === queryText) return;
    const timer = setTimeout(() => {
      const params = new URLSearchParams(searchParams.toString());
      if (searchInput) params.set('q', searchInput.slice(0, 200));
      else params.delete('q');
      params.delete('offset');
      const qs = params.toString();
      router.replace(qs ? `/runs?${qs}` : '/runs', { scroll: false });
    }, 300);
    return () => clearTimeout(timer);
  }, [searchInput, queryText, router, searchParams]);

  // Relative cutoffs anchor when the range is selected (or on manual
  // refresh) and then stay stable across re-renders and pagination, so
  // "Last 24 hours" never silently widens while the page sits open.
  const [anchor, setAnchor] = useState(() => Date.now());
  const updatedSince = useMemo(
    () => (updated.days === null ? null : new Date(anchor - updated.days * 86_400_000).toISOString()),
    [anchor, updated],
  );
  const runsPath = useMemo(() => {
    const params = new URLSearchParams({ offset: String(offset), limit: '25' });
    for (const state of bucket.states) params.append('states', state);
    if (projectId) params.set('project_id', projectId);
    if (queryText) params.set('q', queryText);
    if (attention !== null) params.set('attention', String(attention));
    if (updatedSince) params.set('updated_since', updatedSince);
    return `/run-projections?${params}`;
  }, [bucket, offset, projectId, queryText, attention, updatedSince]);

  const projects = useApi<components['schemas']['ProjectResponse'][]>('/projects');
  const summary = useApi<components['schemas']['DashboardSummary']>('/dashboard/summary');
  const runs = useApi<components['schemas']['RunListPage']>(runsPath);
  const counts = bucketCounts(summary.value);
  // Summary counts cover every run. While any other filter narrows the list
  // they would describe a different set, so hide them rather than imply
  // they are counts of what is on screen.
  const showCounts = Boolean(summary.value) && !projectId && !queryText
    && attention === null && updated.id === 'any';

  return <><h1>Runs</h1><p><Link href="/runs/new">New run</Link></p>
    <div role="group" aria-label="Run status">
      {RUN_BUCKETS.map(candidate => <button key={candidate.id} type="button" aria-pressed={bucket.id === candidate.id}
        onClick={() => update({ bucket: candidate.id === 'all' ? null : candidate.id, offset: null })}>
        {candidate.label}{showCounts ? ` (${counts[candidate.id] ?? 0})` : ''}
      </button>)}
    </div>
    <div className="form-grid">
      <label>Search runs<input type="search" aria-label="Search runs" placeholder="Task title or project" maxLength={200} value={searchInput}
        onChange={event => setSearchInput(event.target.value)} /></label>
      <label>Project<select value={projectId} onChange={event => update({ project_id: event.target.value || null, offset: null })}>
        <option value="">All projects</option>{projects.value?.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
      <label>Attention<select value={attention === null ? '' : String(attention)} onChange={event => update({ attention: event.target.value || null, offset: null })}>
        <option value="">Any attention</option><option value="true">Needs attention</option><option value="false">No attention required</option></select></label>
      <label>Updated<select value={updated.id} onChange={event => { setAnchor(Date.now()); update({ updated: event.target.value === 'any' ? null : event.target.value, offset: null }); }}>
        {UPDATED_OPTIONS.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}</select></label>
    </div>
    {projects.failed && <p role="alert">Project filters unavailable. <button onClick={projects.refresh}>Retry projects</button></p>}
    <button disabled={runs.loading} onClick={() => { setAnchor(Date.now()); runs.refresh(); }}>Refresh runs</button>
    {runs.loading && <LoadingStatus>Loading runs…</LoadingStatus>}
    {runs.failed && <p role="alert">Runs unavailable. <button onClick={runs.refresh}>Retry</button></p>}
    {runs.value && <RunList items={runs.value.items} />}
    <nav aria-label="Run pages"><button disabled={!offset || runs.loading} onClick={() => update({ offset: offset <= 25 ? null : String(offset - 25) })}>Previous</button>
      <button disabled={!runs.value?.truncated || runs.loading} onClick={() => update({ offset: String(offset + 25) })}>Next</button></nav>
  </>;
}

export default function RunsPage() {
  return <Suspense fallback={<LoadingStatus>Loading runs…</LoadingStatus>}><RunsPageContent /></Suspense>;
}
