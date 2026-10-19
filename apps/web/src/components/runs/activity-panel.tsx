'use client';
import { LoadingStatus } from '@/components/ui/loading-status';
import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { describeActivity, safeExactTime, type ActivityDescription } from '@/lib/activity-presentation';
import { RelativeTime } from '@/components/runs/relative-time';
import { StatusBadge } from '@/components/ui/status-badge';
import type { Tone } from '@/components/ui/tone';

function operationStatusTone(status: string): Tone {
  switch (status) {
    case 'SUCCEEDED':
      return 'success';
    case 'FAILED':
    case 'NEEDS_RECONCILIATION':
      return 'danger';
    case 'PENDING':
      return 'warning';
    default:
      return 'neutral';
  }
}

function isActivityAlert(item: components['schemas']['AuditItem'], description: ActivityDescription) {
  return description.status === 'failed' || description.status === 'denied' ||
    (description.status === 'unknown' && /denied|fail|error/.test(item.event_type));
}

function RunActivityItem({ item, desc }: { item: components['schemas']['AuditItem']; desc: ActivityDescription }) {
  const isFailure = isActivityAlert(item, desc);
  const sourceTone: Tone = item.source === 'operator' ? 'warning' : 'info';

  return (
    <article className="audit-card" data-activity-status={desc.status} style={isFailure ? { borderColor: 'var(--danger)', background: 'var(--surface-muted)' } : undefined}>
      <div className="audit-card-header">
        <div>
          <h2>{desc.title}</h2>
          <div className="audit-card-meta">
            <span><RelativeTime timestamp={item.created_at} /> ({safeExactTime(item.created_at)})</span>
            <StatusBadge label={`Source: ${item.source}`} tone={sourceTone} />
            {desc.outcome && <StatusBadge label={desc.outcome} tone={desc.tone} />}
            <span>Actor: {item.actor_class}{item.actor_id ? ` · ${item.actor_id}` : ' · No actor ID'}</span>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <a
            href={item.source === 'run' ? `/api/audit/run-events/${item.id}` : `/api/audit/${item.id}`}
            className="button"
            data-variant="quiet"
            style={{ padding: '6px 12px', fontSize: '0.8125rem' }}
          >
            Event evidence
          </a>
        </div>
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px 16px', fontSize: '0.8125rem', marginTop: '6px' }}>
        <span>Event: <code>{item.event_type}</code></span>
      </div>

      {item.operations.length > 0 && (
        <div className="audit-operations" style={{ marginTop: '8px' }}>
          {item.operations.map(operation => (
            <div key={operation.id} className="audit-operation-row">
              <span style={{ fontWeight: 600 }}>{operation.kind}</span>
              <code>{operation.id}</code>
              <StatusBadge label={`Current status: ${operation.status}`} tone={operationStatusTone(operation.status)} />
            </div>
          ))}
        </div>
      )}

      <details className="disclosure-card text-xs" style={{ marginTop: '8px' }}>
        <summary className="disclosure-summary cursor-pointer">
          Event ID & recorded payload
        </summary>
        <div className="disclosure-body pt-2">
          <p style={{ marginBottom: '6px' }}>Event ID: <code>{item.id}</code></p>
          <pre style={{ margin: 0, maxHeight: '240px', overflowY: 'auto' }}>{JSON.stringify(item.payload, null, 2)}</pre>
        </div>
      </details>
    </article>
  );
}

export function ActivityPanel({ runId }: { runId: string }) {
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState<'all' | 'failures' | 'routine'>('all');
  const activity = useApi<components['schemas']['ListPage_AuditItem_']>(`/audit?run_id=${encodeURIComponent(runId)}&offset=${offset}&limit=25`);

  const items = activity.value?.items ?? [];
  const filteredItems = items.map(item => ({ item, description: describeActivity(item) })).filter(({ item, description }) => {
    if (filter === 'all') return true;
    const isAlert = isActivityAlert(item, description);
    return filter === 'failures' ? isAlert : !isAlert;
  });

  return (
    <section aria-label="Run activity">
      <h2>Activity</h2>
      <p>Newest persisted events first. Includes related task actions. Operation statuses are current; event evidence preserves the original record.</p>

      <div className="activity-controls" style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '12px', marginBottom: '16px' }}>
        <button onClick={activity.refresh} disabled={activity.loading}>Refresh activity</button>
        <label className="text-sm font-medium" style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          Filter:
          <select value={filter} onChange={e => setFilter(e.target.value as 'all' | 'failures' | 'routine')}>
            <option value="all">All events ({items.length})</option>
            <option value="failures">Failures & alerts</option>
            <option value="routine">Routine operations</option>
          </select>
        </label>
      </div>

      {activity.loading && <LoadingStatus>Loading activity…</LoadingStatus>}
      {activity.failed && <p role="alert">Activity unavailable. <button onClick={activity.refresh}>Retry</button></p>}
      {activity.value && (
        <>
          {!items.length ? (
            <p>No recorded activity on this page.</p>
          ) : !filteredItems.length ? (
            <p>No events match the selected activity filter on this page.</p>
          ) : (
            filteredItems.map(({ item, description }) => <RunActivityItem key={`${item.source}:${item.id}`} item={item} desc={description} />)
          )}
        </>
      )}
      <nav aria-label="Activity pages">
        <button disabled={offset === 0 || activity.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Newer activity</button>
        <button disabled={!activity.value?.truncated || activity.loading} onClick={() => setOffset(offset + 25)}>Older activity</button>
      </nav>
    </section>
  );
}
