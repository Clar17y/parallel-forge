'use client';

import type { components } from '@/lib/api/schema';

type QuotaStatus = components['schemas']['QuotaStatusResponse'];

const when = (value: string | null | undefined) => value ? new Date(value).toLocaleString() : 'Unknown';

export function QuotaStatusList({ statuses }: { statuses: QuotaStatus[] }) {
  if (statuses.length === 0) return <p>No shared quota pools reported.</p>;
  return <div aria-label="Provider quota pools" className="space-y-2">
    {statuses.map(status => <QuotaStatusCard key={`${status.provider}:${status.account}:${status.pool}`} status={status} />)}
  </div>;
}

export function QuotaStatusCard({ status }: { status: QuotaStatus }) {
  return <article className="rounded border border-slate-300 p-3">
      <h3 className="font-semibold">{status.provider} / {status.account} / {status.pool}</h3>
      <p>Status: {status.status}</p>
      <p>Reason: {status.reason || 'Unknown'}</p>
      <p>Observed: {when(status.observed_at)}</p>
      <p>{status.reset_at ? `Known reset: ${when(status.reset_at)}` : `Next probe: ${when(status.next_eligible_at)}`}</p>
      {status.probe_attempt_id ? <p>Probe in flight: {status.probe_attempt_id}</p> : null}
      {status.status === 'eligible' ? <p>Eligible for one recovery attempt; remaining allowance is unknown.</p> : null}
    </article>;
}
