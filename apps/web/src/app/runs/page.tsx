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

  // App Router navigations commit asynchronously, so consecutive patches
  // must compose on the last issued query string rather than the last
  // committed one — otherwise a second rapid patch drops the first. Every
  // self-issued string stays in a chain: when one lands, it and every
  // superseded entry before it retire while newer entries stay pending, so
  // an intermediate commit never drops a newer patch; a committed URL this
  // page never issued is external navigation and retires the whole chain.
  // A repeated string matches its latest issuance: any landing in between
  // would have been observed (and retired entries) before it could be
  // issued again, so an observed repeat is the final coalesced navigation.
  const committedSearch = searchParams.toString();
  const [issuedSearches, setIssuedSearches] = useState<string[]>([]);
  const [syncedSearch, setSyncedSearch] = useState(committedSearch);
  if (committedSearch !== syncedSearch) {
    setSyncedSearch(committedSearch);
    const issuedAt = issuedSearches.lastIndexOf(committedSearch);
    if (issuedAt >= 0) setIssuedSearches(issuedSearches.slice(issuedAt + 1));
    else if (issuedSearches.length) setIssuedSearches([]);
  }
  const effectiveSearch = issuedSearches.at(-1) ?? committedSearch;
  const issueParams = (params: URLSearchParams) => {
    const qs = params.toString();
    setIssuedSearches(previous => [...previous, qs]);
    router.replace(qs ? `/runs?${qs}` : '/runs', { scroll: false });
  };

  const update = (patch: Record<string, string | null>) => {
    const params = new URLSearchParams(effectiveSearch);
    for (const [key, value] of Object.entries(patch)) {
      if (value) params.set(key, value);
      else params.delete(key);
    }
    issueParams(params);
  };

  const [searchInput, setSearchInput] = useState(queryText);
  // Keep the input following the URL (back/forward navigation) without an
  // effect: adjust state during render when the param changes externally.
  // Self-issued navigations are tracked separately — when one of those lands
  // late it must not clobber characters typed after it was dispatched. A
  // landing retires its marker and every superseded marker before it (their
  // navigations never committed), and an external landing retires them all,
  // so returning through history to any of those values later counts as
  // external again. Repeated values match their latest marker, as with the
  // issued-search chain above.
  const [syncedQuery, setSyncedQuery] = useState(queryText);
  const [issuedQueries, setIssuedQueries] = useState<string[]>([]);
  if (queryText !== syncedQuery) {
    setSyncedQuery(queryText);
    const issuedAt = issuedQueries.lastIndexOf(queryText);
    if (issuedAt >= 0) setIssuedQueries(issuedQueries.slice(issuedAt + 1));
    else {
      if (issuedQueries.length) setIssuedQueries([]);
      setSearchInput(queryText);
    }
  }
  useEffect(() => {
    const next = searchInput.slice(0, 200);
    const baseQuery = new URLSearchParams(effectiveSearch).get('q') ?? '';
    if (next === baseQuery) return;
    const timer = setTimeout(() => {
      setIssuedQueries(previous => [...previous, next]);
      const params = new URLSearchParams(effectiveSearch);
      if (next) params.set('q', next);
      else params.delete('q');
      params.delete('offset');
      issueParams(params);
    }, 300);
    return () => clearTimeout(timer);
    // issueParams reads this render's committed params by design.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchInput, queryText, effectiveSearch]);

  // Relative cutoffs anchor when the range is selected (or on manual
  // refresh) and then stay stable across re-renders and pagination, so
  // "Last 24 hours" never silently widens while the page sits open. The
  // range can also change through the URL (back/forward, sidebar links)
  // without touching the select, so re-anchor whenever the URL-derived
  // range id changes, however it changed.
  const [anchor, setAnchor] = useState(() => Date.now());
  const [syncedUpdatedId, setSyncedUpdatedId] = useState(updated.id);
  if (updated.id !== syncedUpdatedId) {
    setSyncedUpdatedId(updated.id);
    setAnchor(() => Date.now());
  }
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
    <button disabled={runs.loading} onClick={() => { setAnchor(Date.now()); runs.refresh(); summary.refresh(); }}>Refresh runs</button>
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
