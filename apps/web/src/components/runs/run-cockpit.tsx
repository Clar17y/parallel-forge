'use client';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { api } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';
import { useRunEvents } from '@/hooks/use-run-events';
import { RunControls } from './run-controls';
import { PlanPanel } from './plan-panel';
import { PhaseTimeline } from './phase-timeline';
import { ChecksPanel } from './checks-panel';
import { ReviewPanel } from './review-panel';
import { ActivityPanel } from './activity-panel';
import { UsagePanel } from './usage-panel';
import { SecurityPanel } from './security-panel';
import { ChangesPanel } from './changes-panel';

import Link from 'next/link';
import { ExternalLink, GitBranch, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ChecksSummary } from './checks-summary';
import { readableLabel } from './run-presentation';
import { RunContext } from './run-context';
import { RunStatusBanner } from './run-status-banner';
import { WorkflowProgress } from './workflow-progress';

const SECTIONS = ['overview', 'plan', 'changes', 'checks', 'review', 'tasks', 'activity', 'usage', 'security'] as const;
type Section = (typeof SECTIONS)[number];

type Projection = components['schemas']['RunProjection'];
export function RunCockpit({ initial, tasks }: { initial: Projection; tasks?: ReactNode }) {
  const [value, setValue] = useState(initial);
  const [section, setSection] = useState<Section>('overview');
  const [failed, setFailed] = useState(false);
  const [reading, setReading] = useState(false);
  const [after] = useState(() => String(Math.max(0, ...initial.latest_events.map(event => event.sequence))));
  const owner = useRef<AbortController | null>(null);
  const pending = useRef<Promise<Projection> | null>(null);
  const queued = useRef(false);
  useEffect(() => {
    const controller = new AbortController(); owner.current = controller;
    return () => controller.abort();
  }, []);
  const runId = initial.run.id;
  const refresh = useCallback(function refresh(): Promise<Projection> {
    if (pending.current) { queued.current = true; return pending.current; }
    const signal = owner.current?.signal;
    if (!signal || signal.aborted) return Promise.reject(new Error('Cockpit closed'));
    setReading(true);
    const request = api<Projection>(`/runs/${runId}/projection`, { signal, cache: 'no-store' }).then(next => {
      signal.throwIfAborted();
      if (!next || next.run.id !== runId) throw new Error('Projection unavailable');
      setValue(previous => next.run.version >= previous.run.version ? next : previous);
      setFailed(false);
      return next;
    }).catch(error => { if (!signal.aborted) setFailed(true); throw error; });
    pending.current = request;
    void request.finally(() => {
      if (pending.current !== request) return;
      pending.current = null;
      if (!signal.aborted) {
        setReading(false);
        if (queued.current) { queued.current = false; void refresh().catch(() => {}); }
      }
    }).catch(() => {});
    return request;
  }, [runId]);
  const connection = useRunEvents(runId, after, () => { void refresh().catch(() => {}); });
  const sectionNavigation = useRef<HTMLElement>(null);
  const hasTasks = Boolean(tasks);
  useEffect(() => {
    if (hasTasks && window.location.hash === '#subscription-tasks') setSection('tasks');
  }, [hasTasks]);
  const openSection = (next: 'checks' | 'activity' | 'tasks') => {
    setSection(next);
    sectionNavigation.current?.querySelector<HTMLButtonElement>(`[data-section="${next}"]`)?.focus();
  };
  const stale = failed || connection !== 'connected';
  const sections = hasTasks ? SECTIONS : SECTIONS.filter((item): item is Exclude<Section, 'tasks'> => item !== 'tasks');
  return <div className="run-page">
    <header className="run-heading">
      <div className="run-identity"><h1>{value.task.title}</h1>
        <div className="run-metadata">
          <Link href={`/projects/${value.project.id}`}>{value.project.name}</Link>
          <span><GitBranch aria-hidden="true" />{value.resource.branch_name ?? 'Branch not yet created'}</span>
          <span>Run <code>{value.run.id.slice(0, 8)}</code></span>
          <span>Head <code>{value.candidate.commit?.slice(0, 7) ?? 'not yet recorded'}</code></span>
        </div>
      </div>
      <div className="run-heading-actions">
        {value.pull_request && <a className="button" href={`https://github.com/${value.pull_request.repository}/pull/${value.pull_request.number}`} target="_blank" rel="noreferrer">View PR #{value.pull_request.number}<ExternalLink aria-hidden="true" /></a>}
        <RunControls projection={value} onRefresh={refresh} disabled={stale} />
        <Button variant="quiet" onClick={() => void refresh().catch(() => {})} disabled={reading}><RefreshCw aria-hidden="true" />Refresh run</Button>
      </div>
    </header>
    {failed && <p className="run-notice" role="alert">Current run state could not be refreshed. Actions are disabled until a successful refresh.</p>}
    <RunStatusBanner projection={value} stale={stale} />
    <p className="meta" role="status">Events: {connection}{connection !== 'connected' && ' · Live updates are unavailable. Actions stay disabled until the event connection is restored.'}</p>
    {value.security.runner_mode === 'trusted_host' && <p className="run-notice">Trusted host · unsandboxed execution. Commands are not isolated in Docker.</p>}
    {value.pull_request && <section className="panel run-pr-outcome" aria-label="PR / Merge"><h2>PR / Merge</h2>
      {value.pull_request.merge_sha ? <><h3>Recorded merge commit</h3><code>{value.pull_request.merge_sha}</code></> : <>
        {value.pull_request.queue_admission === 'accepted' && <p>Queue admission recorded. Forge has not recorded a completed merge. Admission does not confirm that the PR is still in the queue.</p>}
        {value.pull_request.queue_admission === 'pending' && <p>Queue admission pending.</p>}
        {value.pull_request.queue_admission === 'rejected' && <p>Queue admission was rejected. Review the activity and current GitHub protections before taking further action.</p>}
        {value.pull_request.queue_admission === 'uncertain' && <p>Queue admission outcome is uncertain. Check the PR on GitHub and preserve the existing operation for reconciliation before attempting another merge.</p>}
        {!value.pull_request.queue_admission && <p>No completed merge recorded.</p>}
        {value.run.state === 'AWAITING_HUMAN_INTERVENTION' && <p>Manual attention required. Review the recorded reason and the PR on GitHub. This page does not authorize another merge or removal from the queue.</p>}
      </>}
    </section>}
    <WorkflowProgress projection={value} stale={stale} />
    <nav ref={sectionNavigation} className="run-sections" aria-label="Run sections">
      {sections.map(item => <button key={item} type="button" data-section={item} aria-pressed={section === item} aria-controls={item === 'tasks' ? 'run-tasks-panel' : 'run-section-panel'}
        onClick={() => setSection(item)}>{readableLabel(item)}</button>)}
    </nav>
    <div id="run-section-panel" className={section === 'overview' ? undefined : 'run-tab-content'} hidden={section === 'tasks'}>
      {section === 'plan' ? <PlanPanel projection={value} /> : section === 'checks' ? <ChecksPanel key={`${value.run.id}:${value.run.version}`} projection={value} /> : section === 'review' ? <ReviewPanel projection={value} /> : section === 'changes' ? <ChangesPanel projection={value} /> : section === 'security' ? <SecurityPanel key={`${value.run.id}:${value.latest_events.at(-1)?.sequence ?? 0}`} projection={value} /> : section === 'usage' ? <UsagePanel key={`${value.run.id}:${value.latest_events.at(-1)?.sequence ?? 0}`} runId={value.run.id} /> : section === 'activity' ? <ActivityPanel key={`${value.run.id}:${value.latest_events.at(-1)?.sequence ?? 0}`} runId={value.run.id} /> : section === 'overview' ? <div className="run-overview">
        <div className="overview-main"><ChecksSummary projection={value} stale={stale} onInspect={() => openSection('checks')} />
          <PhaseTimeline events={value.latest_events} onInspect={() => openSection('activity')} />
        </div>
        <RunContext projection={value} onTasks={tasks ? () => openSection('tasks') : undefined} />
      </div> : null}
    </div>
    {/* Keep the inspector mounted: switching sections must not discard a pending
        task command's retry identity or restart its existing polling owner. */}
    {tasks && <div id="run-tasks-panel" className="run-tab-content" hidden={section !== 'tasks'}>{tasks}</div>}
  </div>;
}
