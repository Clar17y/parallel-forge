import Link from 'next/link';
import type { components } from '@/lib/api/schema';
import { describeActivity, safeExactTime } from '@/lib/activity-presentation';
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

export function AuditEventCard({ item }: { item: components['schemas']['AuditItem'] }) {
  const desc = describeActivity(item);
  const sourceTone = item.source === 'operator' ? 'warning' : 'info';

  return (
    <article className="audit-card">
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
          <Link
            href={`/audit/${item.source}/${item.id}`}
            className="button"
            data-variant="primary"
            style={{ padding: '6px 12px', fontSize: '0.8125rem' }}
          >
            Event details
          </Link>
          <a
            href={item.source === 'run' ? `/api/audit/run-events/${item.id}` : `/api/audit/${item.id}`}
            className="button"
            data-variant="quiet"
            style={{ padding: '6px 12px', fontSize: '0.8125rem' }}
          >
            Raw JSON
          </a>
        </div>
      </div>

      {(item.run_id || item.project_id) && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px 16px', fontSize: '0.8125rem', overflowWrap: 'anywhere', margin: '4px 0' }}>
          {item.run_id && <span><Link href={`/runs/${item.run_id}`}>Run {item.run_id}</Link></span>}
          {item.project_id && <span><Link href={`/projects/${item.project_id}`}>Project {item.project_id}</Link></span>}
        </div>
      )}

      {item.operations.length > 0 && (
        <div className="audit-operations" style={{ margin: '8px 0' }}>
          {item.operations.map(operation => (
            <div key={operation.id} className="audit-operation-row">
              <span style={{ fontWeight: 600 }}>{operation.kind}</span>
              <StatusBadge label={`Current status: ${operation.status}`} tone={operationStatusTone(operation.status)} />
            </div>
          ))}
        </div>
      )}

      <details className="disclosure-card">
        <summary className="disclosure-summary">Recorded event fields</summary>
        <div className="disclosure-body" style={{ display: 'grid', gap: '8px', fontSize: '0.8125rem', paddingTop: '8px' }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px 16px', overflowWrap: 'anywhere' }}>
            <span>Event: <code>{item.event_type}</code></span>
            <span>ID: <code>{item.id}</code></span>
            {item.operations.map(operation => (
              <span key={operation.id}>Op ID: <code>{operation.id}</code></span>
            ))}
          </div>
          <pre className="evidence-scroll" style={{ margin: 0 }}>{JSON.stringify(item.payload, null, 2)}</pre>
        </div>
      </details>
    </article>
  );
}
