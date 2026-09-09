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
import { ChecksPanel } from './checks-panel';
import { ReviewPanel } from './review-panel';
import { ActivityPanel } from './activity-panel';
import { UsagePanel } from './usage-panel';
import { SecurityPanel } from './security-panel';
import { ChangesPanel } from './changes-panel';

type Projection = components['schemas']['RunProjection'];
export function RunCockpit({ initial }: { initial: Projection }) {
  const [value, setValue] = useState(initial);
  const [section, setSection] = useState<'overview' | 'plan' | 'checks' | 'review' | 'activity' | 'usage' | 'security' | 'changes'>('overview');
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
    {value.recovery_hold && <p role="alert">Recovery needs attention: an earlier operation has an unresolved outcome. Resume and resource teardown are held until its evidence is reconciled. Review the activity before taking further action.</p>}
    <button onClick={() => void refresh().catch(() => {})} disabled={reading}>Refresh run</button>
    <dl><dt>Branch</dt><dd>{value.resource.branch_name ?? 'Not yet created'}{value.resource.branch_removed && ' — removal recorded'}</dd>
      <dt>Worktree</dt><dd>{value.resource.worktree_path ?? 'Not yet created'}</dd>
      <dt>Database</dt><dd>{value.resource.database_state === 'DISABLED' ? 'Not configured' : value.resource.database_state}</dd>
      <dt>Head</dt><dd>{value.candidate.commit ?? 'No candidate yet'}</dd>
      <dt>Base</dt><dd>{value.run.base_sha ?? 'Unavailable'}</dd>
      <dt>Next gate</dt><dd>{value.next_gate ?? 'None'}</dd>
      {value.pull_request && <><dt>Pull request</dt><dd><a href={`https://github.com/${value.pull_request.repository}/pull/${value.pull_request.number}`} target="_blank" rel="noreferrer">#{value.pull_request.number}</a></dd></>}
    </dl>
    {value.pull_request && <section aria-label="PR / Merge"><h2>PR / Merge</h2>
      {value.pull_request.merge_sha ? <><h3>Recorded merge commit</h3><code>{value.pull_request.merge_sha}</code></> : <>
        {value.pull_request.queue_admission === 'accepted' && <p>Queue admission recorded. Forge has not recorded a completed merge. Admission does not confirm that the PR is still in the queue.</p>}
        {value.pull_request.queue_admission === 'pending' && <p>Queue admission pending.</p>}
        {value.pull_request.queue_admission === 'rejected' && <p>Queue admission was rejected. Review the activity and current GitHub protections before taking further action.</p>}
        {value.pull_request.queue_admission === 'uncertain' && <p>Queue admission outcome is uncertain. Check the PR on GitHub and preserve the existing operation for reconciliation before attempting another merge.</p>}
        {!value.pull_request.queue_admission && <p>No completed merge recorded.</p>}
        {value.run.state === 'AWAITING_HUMAN_INTERVENTION' && <p>Manual attention required. Review the recorded reason and the PR on GitHub. This page does not authorize another merge or removal from the queue.</p>}
      </>}
    </section>}
    <RunControls projection={value} onRefresh={refresh} disabled={failed || connection !== 'connected'} />
    <nav aria-label="Run sections"><button aria-pressed={section === 'overview'} onClick={() => setSection('overview')}>Overview</button>
      <button aria-pressed={section === 'plan'} onClick={() => setSection('plan')}>Plan</button>
      <button aria-pressed={section === 'checks'} onClick={() => setSection('checks')}>Checks</button>
      <button aria-pressed={section === 'review'} onClick={() => setSection('review')}>Review</button>
      <button aria-pressed={section === 'activity'} onClick={() => setSection('activity')}>Activity</button>
      <button aria-pressed={section === 'usage'} onClick={() => setSection('usage')}>Usage</button>
      <button aria-pressed={section === 'security'} onClick={() => setSection('security')}>Security</button>
      <button aria-pressed={section === 'changes'} onClick={() => setSection('changes')}>Changes</button></nav>
    {section === 'plan' ? <PlanPanel projection={value} /> : section === 'checks' ? <ChecksPanel key={`${value.run.id}:${value.run.version}`} projection={value} /> : section === 'review' ? <ReviewPanel projection={value} /> : section === 'changes' ? <ChangesPanel projection={value} /> : section === 'security' ? <SecurityPanel key={`${value.run.id}:${value.latest_events.at(-1)?.sequence ?? 0}`} projection={value} /> : section === 'usage' ? <UsagePanel key={`${value.run.id}:${value.latest_events.at(-1)?.sequence ?? 0}`} runId={value.run.id} /> : section === 'activity' ? <ActivityPanel key={`${value.run.id}:${value.latest_events.at(-1)?.sequence ?? 0}`} runId={value.run.id} /> : <>
      <AutonomyBanner projection={value} />
      <section><h2>Agent activity</h2>{Object.values(value.agents).map(agent => <AgentStatus key={agent.role} agent={agent} />)}</section>
      <section><h2>Remaining remediation</h2><p>Local: {value.budgets.local_remediation_remaining} / {value.budgets.local_remediation_limit} · Remote: {value.budgets.remote_remediation_remaining} / {value.budgets.remote_remediation_limit}</p></section>
      <PhaseTimeline events={value.latest_events} />
    </>}
  </>;
}
