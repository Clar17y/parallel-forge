'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useApi } from '@/hooks/use-api';
import { QuotaStatusCard, QuotaStatusList } from './quota-status';
import { TaskControls, TaskControlSummary } from './task-controls';
import type { components } from '@/lib/api/schema';

type TaskPage = components['schemas']['SubscriptionTaskPage'];
type AttemptPage = components['schemas']['SubscriptionAttemptPage'];
type Route = components['schemas']['AttemptRoute'];
const pageSize = 25;
const unknown = (value: number | null | undefined) => value == null ? 'Unknown' : value.toLocaleString();
const routeLabel = (route: Route) => `${route.provider} / ${route.client} / ${route.model} · ${route.effort} · ${route.auth_mode} · ${route.billing_mode}`;

export function TaskInspector({ runId }: { runId: string }) {
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const tasks = useApi<TaskPage>(`/runs/${runId}/subscription-tasks?offset=${offset}&limit=${pageSize}`, { refreshIntervalMs: 5000, keepPreviousOnRefresh: true });
  const movePage = (next: number) => { setSelected(null); setOffset(next); };
  return <section id="subscription-tasks" aria-label="Subscription tasks" className="my-6 min-w-0 space-y-4 rounded-lg border border-slate-300 p-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 className="text-lg font-semibold">Subscription tasks</h2>
      <button type="button" onClick={tasks.refresh}>Refresh tasks</button>
    </div>
    {tasks.refreshing && !tasks.loading ? <p role="status">Refreshing task state…</p> : null}
    {tasks.loading ? <p role="status">Loading tasks…</p> : tasks.failed ?
      <p role="alert">Task inspection unavailable. Use Refresh tasks to retry.</p> :
      tasks.value && !tasks.value.subscription ? <p>Legacy run: subscription tasks do not apply.</p> : tasks.value ? <>
        <p>Candidate: {tasks.value.candidate_state ?? 'Unknown'} · epoch {unknown(tasks.value.candidate_epoch)}</p>
        <CapacityStatus capacity={tasks.value.capacity} />
        <p><Link href="/subscription-profiles#client-setup">Inspect worker registration and client setup</Link></p>
        <QuotaStatusList statuses={tasks.value.quota_statuses ?? []} />
        {tasks.value.tasks.length === 0 ? <p>No tasks on this page.</p> :
          <ul className="space-y-3">{tasks.value.tasks.map(task => <li key={task.task_id} className="min-w-0 space-y-1 rounded border border-slate-300 p-3 [overflow-wrap:anywhere]">
            <button type="button" aria-expanded={selected === task.task_id}
              aria-label={`Inspect ${task.purpose} task ${task.task_id}`}
              onClick={() => setSelected(selected === task.task_id ? null : task.task_id)}
              className="text-left font-semibold underline underline-offset-4">
              {task.purpose} · {task.state}
            </button>
            <p className="text-sm">Task {task.task_id}</p>
            <p>{task.parent_task_id ? `Parent: ${task.parent_task_id}` : 'Root task'}</p>
            {task.dependency_task_ids.length > 0 ? <p>Depends on: {task.dependency_task_ids.join(', ')}</p> : null}
            <p>Owned paths: <span>{task.owned_paths.join(', ') || 'None'}</span></p>
            {selected !== task.task_id ? <TaskControlSummary task={task} /> : null}
            <p>{task.unsettled_effects} unsettled effect(s)</p>
            {task.capacity_waits?.length ? <p>Capacity limits reached: {task.capacity_waits.map(value => value === 'run' ? 'this run' : value).join(', ')}</p> : null}
            {task.quota_status ? <QuotaStatusCard status={task.quota_status} /> : null}
            {task.effective_route ? <p>Selected route: {routeLabel(task.effective_route)}{task.fallback_selected ? ' · approved fallback selected' : ' · preferred route'}</p> : null}
            {selected === task.task_id ? <>
              <TaskControls key={`${task.task_id}-controls`} runId={runId}
                runVersion={tasks.value!.run_version ?? -1} runAllowsExecution={tasks.value!.run_allows_execution ?? false}
                runIsTerminal={tasks.value!.run_is_terminal ?? false} task={task} onRefresh={tasks.refresh} />
              <Attempts key={task.task_id} runId={runId} taskId={task.task_id} />
            </> : null}
          </li>)}</ul>}
        <nav aria-label="Task pages" className="flex flex-wrap gap-4">
          <button type="button" disabled={offset === 0} onClick={() => movePage(Math.max(0, offset - pageSize))}>Previous tasks</button>
          <span>Page {Math.floor(offset / pageSize) + 1}</span>
          <button type="button" disabled={!tasks.value.has_more} onClick={() => movePage(offset + pageSize)}>Next tasks</button>
        </nav>
      </> : null}
  </section>;
}

function CapacityStatus({ capacity }: { capacity: TaskPage['capacity'] }) {
  if (!capacity) return <p>Execution capacity: Unknown</p>;
  return <div aria-label="Execution capacity" className="space-y-1 text-sm">
    <p>Host: {capacity.host.active}/{capacity.host.limit} · This run: {capacity.run.active}/{capacity.run.limit}</p>
    {capacity.providers.map((provider, index) => <p key={index}>{provider.provider}: {provider.active}/{provider.limit}</p>)}
    <p>Least recently served run, then oldest task. Quota, ownership and approvals still apply.</p>
    <p>Capacity observed: <time dateTime={capacity.observed_at}>{capacity.observed_at}</time></p>
  </div>;
}

function Attempts({ runId, taskId }: { runId: string; taskId: string }) {
  const [offset, setOffset] = useState(0);
  const attempts = useApi<AttemptPage>(`/runs/${runId}/subscription-tasks/${taskId}/attempts?offset=${offset}&limit=${pageSize}`, { refreshIntervalMs: 5000, keepPreviousOnRefresh: true });
  if (attempts.loading) return <p role="status">Loading attempts…</p>;
  if (!attempts.value) return <p role="alert">Attempts unavailable. <button onClick={attempts.refresh}>Retry attempts</button></p>;
  return <div className="space-y-3 border-t border-slate-300 pt-3">
    <button type="button" onClick={attempts.refresh}>Refresh attempts</button>
    {attempts.value.attempts.length === 0 ? <p>No attempts on this page.</p> :
      attempts.value.attempts.map(attempt => <article key={attempt.attempt_id} className="space-y-2 rounded bg-[#202731] p-3">
        <h3 className="font-semibold">Attempt {attempt.attempt_number} · {attempt.state}</h3>
        <p>Requested: {routeLabel(attempt.requested_route)}</p>
        <p>Effective: {routeLabel(attempt.effective_route)}</p>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          <div><dt>Input tokens</dt><dd>{unknown(attempt.input_tokens)}</dd></div>
          <div><dt>Output tokens</dt><dd>{unknown(attempt.output_tokens)}</dd></div>
          <div><dt>Cached tokens</dt><dd>{unknown(attempt.cached_tokens)}</dd></div>
          <div><dt>Duration (ms)</dt><dd>{unknown(attempt.duration_ms)}</dd></div>
          <div><dt>Tool calls</dt><dd>{unknown(attempt.tool_calls)}</dd></div>
          <div><dt>Named checks</dt><dd>{unknown(attempt.named_checks)}</dd></div>
          <div><dt>API estimate (minor units)</dt><dd>{unknown(attempt.estimated_api_cost_minor)}{attempt.currency ? ` ${attempt.currency}` : ''}</dd></div>
          <div><dt>Quota</dt><dd>{attempt.quota_status === 'unknown' ? 'Unknown' : attempt.quota_status}</dd></div>
        </dl>
      </article>)}
    <nav aria-label="Attempt pages" className="flex flex-wrap gap-4">
      <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>Previous attempts</button>
      <span>Page {Math.floor(offset / pageSize) + 1}</span>
      <button type="button" disabled={!attempts.value.has_more} onClick={() => setOffset(offset + pageSize)}>Next attempts</button>
    </nav>
  </div>;
}
