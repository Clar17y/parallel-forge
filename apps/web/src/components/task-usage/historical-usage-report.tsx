'use client';

import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

type Report = components['schemas']['TaskUsageHistoryResponse'];
type Summary = components['schemas']['UsageHistorySummaryResponse'];
type Filters = { project: string; category: string; provider: string; model: string; effort: string };
const empty: Filters = { project: '', category: '', provider: '', model: '', effort: '' };
const count = (value: number | string | null | undefined) =>
  value == null ? 'Unknown' : (typeof value === 'number' ? value.toLocaleString() : BigInt(value).toLocaleString());
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

export function HistoricalUsageReport({ active = true }: { active?: boolean }) {
  const [draft, setDraft] = useState(empty);
  const [filters, setFilters] = useState(empty);
  const report = useApi<Report>(active ? queryFor(filters) : null);
  const projects = useApi<components['schemas']['ProjectResponse'][]>(active ? '/projects' : null);
  const projectList = Array.isArray(projects.value) ? projects.value : [];
  const update = (field: keyof Filters) => (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setDraft(current => ({ ...current, [field]: event.target.value }));
  const summary = report.value?.overview;
  return <section aria-label="Historical task usage" className="my-6 space-y-4 rounded-lg border border-[var(--border)] p-4">
    <div>
      <h2 className="text-lg font-semibold">Historical task usage</h2>
      <p className="text-sm text-[var(--muted)]">Observed task usage across projects you can access. The pooled overview is advisory and is not a comparable checkpoint reference or a usage allowance.</p>
    </div>
    <form className="grid items-start gap-3 sm:grid-cols-2 lg:grid-cols-3" onSubmit={event => { event.preventDefault(); setFilters(draft); }}>
      <label className="grid gap-1">
        <span className="font-medium">Project filter</span>
        <input aria-label="Project filter" value={draft.project} onChange={update('project')} placeholder="All accessible projects" list="historical-projects-list" className="border border-[var(--border)] rounded p-1 text-sm" />
        <datalist id="historical-projects-list">
          {projectList.map(p => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </datalist>
        {projectList.length > 0 && (
          <select
            aria-label="Authorized project chooser"
            value={projectList.some(p => p.id === draft.project) ? draft.project : ''}
            onChange={e => {
              if (e.target.value) {
                setDraft(current => ({ ...current, project: e.target.value }));
              }
            }}
            className="text-xs border border-[var(--border)] rounded p-1 text-[var(--text)] bg-[var(--surface)]"
          >
            <option value="">Choose an authorized project…</option>
            {projectList.map(p => (
              <option key={p.id} value={p.id}>{p.name} ({p.id})</option>
            ))}
          </select>
        )}
        <span className="text-xs text-[var(--muted)]">Filter by accessible project or enter exact project UUID</span>
      </label>
      <label className="grid gap-1"><span className="font-medium">Task category</span><select aria-label="Task category filter" value={draft.category} onChange={update('category')} className="border border-[var(--border)] rounded p-1 text-sm">
        <option value="">All categories</option><option value="brainstorm">Brainstorming</option><option value="decomposition">Decomposition</option><option value="implementation">Implementation</option>
      </select></label>
      <label className="grid gap-1"><span className="font-medium">Provider filter</span><input aria-label="Provider filter" value={draft.provider} onChange={update('provider')} className="border border-[var(--border)] rounded p-1 text-sm" /></label>
      <label className="grid gap-1"><span className="font-medium">Model filter</span><input aria-label="Model filter" value={draft.model} onChange={update('model')} className="border border-[var(--border)] rounded p-1 text-sm" /></label>
      <label className="grid gap-1"><span className="font-medium">Effort filter</span><input aria-label="Effort filter" value={draft.effort} onChange={update('effort')} className="border border-[var(--border)] rounded p-1 text-sm" /></label>
      <div className="flex flex-wrap gap-2 self-end">
        <button type="submit" className="rounded border border-[var(--border)] px-3 py-2 text-sm font-medium">Apply filters</button>
        <button type="button" disabled={report.refreshing} onClick={report.refresh} className="rounded border border-[var(--border)] px-3 py-2 text-sm font-medium">
          {report.refreshing ? 'Refreshing history…' : 'Refresh history'}
        </button>
      </div>
    </form>
    {report.loading ? <p role="status">Loading measured usage history…</p> : null}
    {report.failed ? <p role="alert">Historical usage is unavailable. The report has not established a baseline.</p> : null}
    {summary ? <>
      <article aria-label="Pooled overview" className="space-y-2 rounded border border-[var(--border)] p-3">
        <h3 className="font-semibold">Pooled overview · mixed routes</h3>
        <p>{summary.status === 'ready' ? `Mean ${count(summary.mean_tokens)} tokens · Median ${count(summary.median_tokens)} · P90 ${count(summary.p90_tokens)}` : 'Mean Unknown · Median Unknown · P90 Unknown'}</p>
        <p>{count(summary.sample_count)} measured samples · {pct(summary.measured_coverage)} · {windowLabel(summary, report.value?.filters.window_days)}</p>
        <p>Coverage: {count(summary.coverage_count)} measured of {count(summary.eligible_count)} eligible · {count(summary.excluded_count)} excluded. Source: {label(summary.cohort_source)}.</p>
        {summary.project_ids.length ? <p>Projects represented: {summary.project_ids.map(project => <span key={project} className="mr-2">{project}</span>)}</p> : null}
        <p className="text-sm text-[var(--muted)]">Provider, model and effort breakdowns below show the pooled route mix; they are separate marginal views.</p>
        {summary.labels.length ? <p className="text-sm">Report labels: {summary.labels.map(value => label(value)).join(', ')}</p> : null}
        {summary.phase_role_breakdown.length ? <ul aria-label="Phase and role breakdown" className="list-disc pl-5 text-sm">{summary.phase_role_breakdown.map((item, index) =>
          <li key={`${item.phase}:${item.role}:${index}`}>{label(item.phase)} · {label(item.role)} · {count(item.tokens)} tokens</li>)}</ul> : <p className="text-sm text-[var(--muted)]">Phase and role breakdown unavailable.</p>}
      </article>
      <section aria-label="Comparable checkpoint reference" className="rounded border border-[var(--border)] p-3 space-y-2">
        <h3 className="font-semibold">Comparable checkpoint reference</h3>
        {Object.entries(report.value?.comparable_references ?? {}).length ? Object.entries(report.value!.comparable_references).map(([key, reference]) =>
          <Comparable key={key} label={key} summary={reference} projectName={projectList.find(project => project.id === key.split(':')[0])?.name} />) : <p className="text-sm text-[var(--muted)]">No comparable checkpoint reference is available for these filters.</p>}
      </section>
      <Breakdowns report={report.value!} />
      <p className="text-sm text-[var(--muted)]">Report as of {new Date(report.value!.as_of).toLocaleString()}. Figures describe observed history; example usage figures are not measured defaults.</p>
    </> : null}
  </section>;
}

function Comparable({ label: name, summary, projectName }: { label: string; summary: Summary; projectName?: string }) {
  // The API key identifies project, subject kind, task kind, configured route and used route.
  // Route identities are digests, not provider/model names.
  const parts = name.split(':');
  const structuredTitle = parts.length === 5
    ? `${label(parts[2]).replace(/^./, char => char.toUpperCase())} · ${projectName ?? `Project ${parts[0].slice(0, 8)}`}`
    : 'Comparable reference';

  const statusProse = summary.status === 'manual'
    ? `Manual estimate: ${count(summary.reference_tokens)} tokens`
    : summary.status === 'ready'
    ? `Learned reference: ${count(summary.reference_tokens)} tokens · ${summary.sample_count} samples · ${summary.cohort_source}`
    : `Insufficient history · ${count(summary.sample_count)} samples`;

  return (
    <div className="rounded border border-[var(--border)] bg-[var(--surface-muted)] p-2.5 text-sm space-y-1 my-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold text-[var(--text)]">{structuredTitle}</span>
        <span className="text-xs px-2 py-0.5 rounded bg-[var(--surface)] border border-[var(--border)] text-[var(--text)]">
          {summary.status === 'ready' ? 'Ready' : summary.status === 'manual' ? 'Manual' : 'Insufficient history'}
        </span>
      </div>
      {parts.length === 5 ? <p className="text-xs text-[var(--muted)]">{label(parts[1])} · Configured route {parts[3].slice(0, 8)} · Used route {parts[4].slice(0, 8)}</p> : null}
      <p className="text-[var(--text)]">{statusProse}</p>
      <details className="disclosure-card text-xs text-[var(--muted)] mt-1">
        <summary className="disclosure-summary cursor-pointer">Exact key & provenance</summary>
        <div className="disclosure-body pt-1">
          <code className="break-all font-mono text-[var(--text)]">{name}</code>
        </div>
      </details>
    </div>
  );
}

function windowLabel(summary: Summary, windowDays?: string | number | null) {
  const start = summary.window_started_at?.slice(0, 10);
  const end = summary.window_ended_at?.slice(0, 10);
  if (start && end) return `observed ${start} to ${end}`;
  if (windowDays != null) return `${count(windowDays)}-day window; observed dates unknown`;
  return 'all-time window; observed dates unknown';
}

function Breakdowns({ report }: { report: Report }) {
  const groups: [string, Record<string, Summary>][] = [
    ['By task type', report.by_task_type],
    ['By project', report.by_project],
    ['By provider', report.by_provider],
    ['By model', report.by_model],
    ['By effort', report.by_effort],
  ];
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {groups.map(([title, entries]) => (
        <section key={title} aria-label={title} className="rounded border border-[var(--border)] p-3 space-y-2">
          <h3 className="font-semibold text-sm">{title}</h3>
          {Object.entries(entries).length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  <tr className="border-b border-[var(--border)] text-[var(--muted)] uppercase tracking-wider">
                    <th className="py-1.5 pr-2 font-medium">Name</th>
                    <th className="py-1.5 px-2 font-medium">Samples</th>
                    <th className="py-1.5 px-2 font-medium">Mean</th>
                    <th className="py-1.5 px-2 font-medium">Median</th>
                    <th className="py-1.5 px-2 font-medium">P90</th>
                    <th className="py-1.5 pl-2 font-medium">Coverage</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border)]">
                  {Object.entries(entries).map(([name, value]) => {
                    const isReady = value.status === 'ready' && value.mean_tokens !== null;
                    return (
                      <tr key={name} className="align-top">
                        <td className="py-1.5 pr-2 font-medium text-[var(--text)]">{label(name)}</td>
                        <td className="py-1.5 px-2 text-[var(--muted)]">{count(value.sample_count)}</td>
                        {isReady ? (
                          <>
                            <td className="py-1.5 px-2 text-[var(--text)]">{count(value.mean_tokens)}</td>
                            <td className="py-1.5 px-2 text-[var(--text)]">{count(value.median_tokens)}</td>
                            <td className="py-1.5 px-2 text-[var(--text)]">{count(value.p90_tokens)}</td>
                          </>
                        ) : (
                          <td colSpan={3} className="py-1.5 px-2 text-[var(--muted)] italic">
                            Insufficient history to establish baseline
                          </td>
                        )}
                        <td className="py-1.5 pl-2 text-[var(--muted)]">{pct(value.measured_coverage)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-xs text-[var(--muted)]">No grouped measurements.</p>
          )}
        </section>
      ))}
    </div>
  );
}
