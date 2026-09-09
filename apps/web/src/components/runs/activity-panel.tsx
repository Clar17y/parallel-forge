'use client';
import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { AuditEventCard } from '@/components/audit/audit-event-card';

export function ActivityPanel({ runId }: { runId: string }) {
  const [offset, setOffset] = useState(0);
  const activity = useApi<components['schemas']['ListPage_AuditItem_']>(`/audit?run_id=${encodeURIComponent(runId)}&offset=${offset}&limit=25`);
  return <section aria-label="Run activity"><h2>Activity</h2>
    <p>Newest persisted events first. Includes related task actions. Operation statuses are current; event evidence preserves the original record.</p>
    <button onClick={activity.refresh} disabled={activity.loading}>Refresh activity</button>
    {activity.loading && <p role="status">Loading activity…</p>}
    {activity.failed && <p role="alert">Activity unavailable. <button onClick={activity.refresh}>Retry</button></p>}
    {activity.value && <>{!activity.value.items.length && <p>No recorded activity on this page.</p>}
      {activity.value.items.map(item => <AuditEventCard key={`${item.source}:${item.id}`} item={item} />)}</>}
    <nav aria-label="Activity pages"><button disabled={offset === 0 || activity.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Newer activity</button>
      <button disabled={!activity.value?.truncated || activity.loading} onClick={() => setOffset(offset + 25)}>Older activity</button></nav>
  </section>;
}
