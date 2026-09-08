'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';
import { useRunEvents } from '@/hooks/use-run-events';
import { RunControls } from './run-controls';
import { PlanPanel } from './plan-panel';
import { PhaseTimeline } from './phase-timeline';
import { AutonomyBanner } from './autonomy-banner';
import { AgentStatus } from './agent-status';

type Projection = components['schemas']['RunProjection'];
export function RunCockpit({ initial }: { initial: Projection }) {
  const [value, setValue] = useState(initial);
  const [section, setSection] = useState<'overview' | 'plan'>('overview');
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
  return <>
    <h1>{value.task.title}</h1>
    <header><p>{value.project.name} · {value.run.state.replaceAll('_', ' ')}</p><p role="status">Events: {connection}</p></header>
    {failed && <p role="alert">Current run state could not be refreshed. Actions are disabled until a successful refresh.</p>}
    <button onClick={() => void refresh().catch(() => {})} disabled={reading}>Refresh run</button>
    <dl><dt>Branch</dt><dd>{value.resource.branch_name ?? 'Not yet created'}</dd>
      <dt>Worktree</dt><dd>{value.resource.worktree_path ?? 'Not yet created'}</dd>
      <dt>Database</dt><dd>{value.resource.database_state === 'DISABLED' ? 'Not configured' : value.resource.database_state}</dd>
      <dt>Head</dt><dd>{value.candidate.commit ?? 'No candidate yet'}</dd>
      <dt>Base</dt><dd>{value.run.base_sha ?? 'Unavailable'}</dd>
      <dt>Next gate</dt><dd>{value.next_gate ?? 'None'}</dd>
      {value.pull_request && <><dt>Pull request</dt><dd><a href={`https://github.com/${value.pull_request.repository}/pull/${value.pull_request.number}`} target="_blank" rel="noreferrer">#{value.pull_request.number}</a></dd></>}
    </dl>
    <RunControls projection={value} onRefresh={refresh} disabled={failed || reading || connection !== 'connected'} />
    <nav aria-label="Run sections"><button aria-pressed={section === 'overview'} onClick={() => setSection('overview')}>Overview</button>
      <button aria-pressed={section === 'plan'} onClick={() => setSection('plan')}>Plan</button></nav>
    {section === 'plan' ? <PlanPanel projection={value} /> : <>
      <AutonomyBanner projection={value} />
      <section><h2>Agent activity</h2>{Object.values(value.agents).map(agent => <AgentStatus key={agent.role} agent={agent} />)}</section>
      <section><h2>Remaining remediation</h2><p>Local: {value.budgets.local_remediation_remaining} / {value.budgets.local_remediation_limit} · Remote: {value.budgets.remote_remediation_remaining} / {value.budgets.remote_remediation_limit}</p></section>
      <PhaseTimeline events={value.latest_events} />
    </>}
  </>;
}
