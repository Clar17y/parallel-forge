'use client';

import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

type Report = components['schemas']['TaskUsageHistoryResponse'];
type Summary = components['schemas']['UsageHistorySummaryResponse'];
type Filters = { project: string; category: string; provider: string; model: string; effort: string };
const empty: Filters = { project: '', category: '', provider: '', model: '', effort: '' };
const count = (value: number | null | undefined) => value == null ? 'Unknown' : value.toLocaleString();
const pct = (value: number | null | undefined) => value == null ? 'coverage unknown' : `${(value * 100).toFixed(0)}% measured coverage`;
const label = (value: string) => value.replaceAll('_', ' ');

function queryFor(filters: Filters) {
  const query = new URLSearchParams();
  if (filters.project.trim()) query.append('project_id', filters.project.trim());
  if (filters.category === 'brainstorm' || filters.category === 'decomposition') query.append('task_kind', filters.category);
  if (filters.category === 'implementation') { query.append('task_kind', 'implementation'); query.append('subject_kind', 'run'); }
  if (filters.category === 'brainstorm' || filters.category === 'decomposition') query.append('subject_kind', 'authoring_job');
  if (filters.provider.trim()) query.append('provider', filters.provider.trim());
  if (filters.model.trim()) query.append('model', filters.model.trim());
  if (filters.effort.trim()) query.append('effort', filters.effort.trim());
  return `/task-usage-report${query.size ? `?${query}` : ''}`;
}

export function HistoricalUsageReport() {
  const [draft, setDraft] = useState(empty);
  const [filters, setFilters] = useState(empty);
  const report = useApi<Report>(queryFor(filters));
  const update = (field: keyof Filters) => (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setDraft(current => ({ ...current, [field]: event.target.value }));
  const summary = report.value?.overview;
  return <section aria-label="Historical task usage" className="my-6 space-y-4 rounded-lg border border-slate-300 p-4">
    <div>
      <h2 className="text-lg font-semibold">Historical task usage</h2>
      <p>Observed task usage across projects you can access. The pooled overview is advisory and is not a comparable checkpoint reference or a usage allowance.</p>
    </div>
    <form className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" onSubmit={event => { event.preventDefault(); setFilters(draft); }}>
      <label className="grid gap-1">Project filter<input aria-label="Project filter" value={draft.project} onChange={update('project')} placeholder="All accessible projects" /></label>
      <label className="grid gap-1">Task category<select aria-label="Task category filter" value={draft.category} onChange={update('category')}>
        <option value="">All categories</option><option value="brainstorm">Brainstorming</option><option value="decomposition">Decomposition</option><option value="implementation">Implementation</option>
      </select></label>
      <label className="grid gap-1">Provider filter<input aria-label="Provider filter" value={draft.provider} onChange={update('provider')} /></label>
      <label className="grid gap-1">Model filter<input aria-label="Model filter" value={draft.model} onChange={update('model')} /></label>
      <label className="grid gap-1">Effort filter<input aria-label="Effort filter" value={draft.effort} onChange={update('effort')} /></label>
      <div className="flex flex-wrap gap-2 self-end">
        <button type="submit" className="rounded border border-slate-400 px-3 py-2">Apply filters</button>
        <button type="button" disabled={report.refreshing} onClick={report.refresh} className="rounded border border-slate-400 px-3 py-2">
          {report.refreshing ? 'Refreshing history…' : 'Refresh history'}
        </button>
      </div>
    </form>
    {report.loading ? <p role="status">Loading measured usage history…</p> : null}
    {report.failed ? <p role="alert">Historical usage is unavailable. The report has not established a baseline.</p> : null}
    {summary ? <>
      <article aria-label="Pooled overview" className="space-y-2 rounded border border-slate-300 p-3">
        <h3 className="font-semibold">Pooled overview · mixed routes</h3>
        <p>{summary.status === 'ready' ? `Mean ${count(summary.mean_tokens)} tokens · Median ${count(summary.median_tokens)} · P90 ${count(summary.p90_tokens)}` : 'Mean Unknown · Median Unknown · P90 Unknown'}</p>
        <p>{count(summary.sample_count)} measured samples · {pct(summary.measured_coverage)} · {windowLabel(summary, report.value?.filters.window_days)}</p>
        <p>Coverage: {summary.coverage_count} measured of {summary.eligible_count} eligible · {summary.excluded_count} excluded. Source: {label(summary.cohort_source)}.</p>
        {summary.project_ids.length ? <p>Projects represented: {summary.project_ids.map(project => <span key={project} className="mr-2">{project}</span>)}</p> : null}
        <p>Provider, model and effort breakdowns below show the pooled route mix; they are separate marginal views.</p>
        {summary.labels.length ? <p>Report labels: {summary.labels.map(value => label(value)).join(', ')}</p> : null}
        {summary.phase_role_breakdown.length ? <ul aria-label="Phase and role breakdown" className="list-disc pl-5">{summary.phase_role_breakdown.map((item, index) =>
          <li key={`${item.phase}:${item.role}:${index}`}>{label(item.phase)} · {label(item.role)} · {count(item.tokens)} tokens</li>)}</ul> : <p>Phase and role breakdown unavailable.</p>}
      </article>
      <section aria-label="Comparable checkpoint reference" className="rounded border border-slate-300 p-3">
        <h3 className="font-semibold">Comparable checkpoint reference</h3>
        {Object.entries(report.value?.comparable_references ?? {}).length ? Object.entries(report.value!.comparable_references).map(([key, reference]) =>
          <Comparable key={key} label={key} summary={reference} />) : <p>No comparable checkpoint reference is available for these filters.</p>}
      </section>
      <Breakdowns report={report.value!} />
      <p className="text-sm">Report as of {new Date(report.value!.as_of).toLocaleString()}. Figures describe observed history; example usage figures are not measured defaults.</p>
    </> : null}
  </section>;
}

function Comparable({ label: name, summary }: { label: string; summary: Summary }) {
  return <p>{label(name)} · {summary.status === 'manual' ? `Manual estimate: ${count(summary.reference_tokens)} tokens` :
    summary.status === 'ready' ? `Learned reference: ${count(summary.reference_tokens)} tokens` : `Insufficient history · ${count(summary.sample_count)} samples`}
    {summary.status === 'ready' ? ` · ${summary.sample_count} samples · ${summary.cohort_source}` : ''}</p>;
}

function windowLabel(summary: Summary, windowDays?: number | null) {
  const start = summary.window_started_at?.slice(0, 10);
  const end = summary.window_ended_at?.slice(0, 10);
  if (start && end) return `observed ${start} to ${end}`;
  if (windowDays != null) return `${windowDays}-day window; observed dates unknown`;
  return 'all-time window; observed dates unknown';
}

function Breakdowns({ report }: { report: Report }) {
  const groups: [string, Record<string, Summary>][] = [
    ['By task type', report.by_task_type], ['By project', report.by_project], ['By provider', report.by_provider],
    ['By model', report.by_model], ['By effort', report.by_effort],
  ];
  return <div className="grid gap-3 sm:grid-cols-2">{groups.map(([title, entries]) => <section key={title} aria-label={title} className="rounded border border-slate-300 p-3">
    <h3 className="font-semibold">{title}</h3>{Object.entries(entries).length ? <ul className="list-disc pl-5">{Object.entries(entries).map(([name, value]) =>
      <li key={name}>{label(name)} · mean {count(value.mean_tokens)} · median {count(value.median_tokens)} · P90 {count(value.p90_tokens)} · {value.sample_count} samples · {pct(value.measured_coverage)}</li>)}</ul> : <p>No grouped measurements.</p>}
  </section>)}</div>;
}
