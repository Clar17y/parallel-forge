'use client';

import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

type Page = components['schemas']['SubscriptionRuntimeStatusPage'];
const pageSize = 25;
const states = {
  current: 'Current report',
  stale: 'Stale report — current registration is unknown',
  stopped: 'Worker stopped reporting',
};

export function SubscriptionRuntimeStatus() {
  const [offset, setOffset] = useState(0);
  const reports = useApi<Page>(`/subscription-runtime?offset=${offset}&limit=${pageSize}`, {
    refreshIntervalMs: 5000, keepPreviousOnRefresh: true,
  });
  return <section id="client-setup" aria-label="Worker registration" className="my-6 space-y-3 rounded border p-4">
    <h2>Worker registration</h2>
    <p>Registration shows the exact routes a worker can construct. It does not confirm sign-in, model access, tool isolation, allowance-only billing or available quota. Provider checks and approvals still apply before execution.</p>
    <button type="button" disabled={reports.refreshing} onClick={reports.refresh}>Refresh worker registration</button>
    {reports.loading ? <p role="status">Loading worker registration…</p> : reports.failed ?
      <p role="alert">Worker registration unavailable. Refresh to retry; current registration is unknown.</p> : reports.value ? <>
        <p>Observed: <time dateTime={reports.value.observed_at}>{reports.value.observed_at}</time>. Reports become stale after {reports.value.fresh_for_seconds} seconds without renewal.</p>
        {reports.refreshing ? <p role="status">Refreshing worker registration…</p> : null}
        {reports.value.workers.length === 0 ? <p>Worker registration is unknown: no reports on this page. Start the Forge worker to publish its registration.</p> :
          <ul className="space-y-3">{reports.value.workers.map(worker => <li key={worker.worker_instance_id} className="rounded border p-3 [overflow-wrap:anywhere]">
            <h3>{states[worker.state]}</h3>
            <p>Instance {worker.worker_instance_id} · last report <time dateTime={worker.last_seen_at}>{worker.last_seen_at}</time></p>
            {worker.routes.length > 0 ? <ul>{worker.routes.map(route =>
              <li key={JSON.stringify(route)}>{route.provider} / {route.client} / {route.model} · {route.effort} · {route.auth_mode} · {route.billing_mode}</li>)}</ul> :
              worker.state === 'current' ? <p>No subscription routes registered by this worker. Complete client setup and capability verification before launching these routes.</p> : <p>No routes in this historical report.</p>}
          </li>)}</ul>}
        {reports.value.has_more ? <p>This page is not the complete worker inventory. Inspect the next page before drawing conclusions about other workers.</p> : null}
      </> : null}
    <nav aria-label="Worker report pages" className="flex gap-4">
      <button type="button" disabled={offset === 0 || reports.refreshing} onClick={() => setOffset(Math.max(0, offset - pageSize))}>Previous worker reports</button>
      <span>Page {Math.floor(offset / pageSize) + 1}</span>
      <button type="button" disabled={!reports.value?.has_more || reports.refreshing || reports.failed} onClick={() => setOffset(offset + pageSize)}>Next worker reports</button>
    </nav>
  </section>;
}
