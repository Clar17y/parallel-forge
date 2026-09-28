import Link from 'next/link';
import type { components } from '@/lib/api/schema';
import { StatusBadge } from '@/components/ui/status-badge';
import { describeActivity, safeExactTime } from '@/lib/activity-presentation';
import { RelativeTime } from '@/components/runs/relative-time';

export function AuditEventCard({ item }: { item: components['schemas']['AuditItem'] }) {
  const desc = describeActivity(item);

  return (
    <article className="audit-card">
      <div className="audit-card-header">
        <h2>{desc.title}</h2>
        {desc.outcome && (
          <StatusBadge label={desc.outcome} tone={desc.tone} />
        )}
      </div>
      <p>
        <RelativeTime timestamp={item.created_at} /> ({safeExactTime(item.created_at)}) · <code>{item.event_type}</code> · {item.actor_class} · {item.actor_id ?? 'No actor ID'}
      </p>
      {item.run_id && (
        <p>
          <Link href={`/runs/${item.run_id}`}>Run {item.run_id}</Link>
        </p>
      )}
      {item.project_id && (
        <p>
          <Link href={`/projects/${item.project_id}`}>Project {item.project_id}</Link>
        </p>
      )}
      <p>
        <a href={item.source === 'run' ? `/api/audit/run-events/${item.id}` : `/api/audit/${item.id}`}>
          Event evidence
        </a>{' '}
        · <code>{item.id}</code>
      </p>
      {item.operations.map(operation => (
        <p key={operation.id}>
          {operation.kind} · <code>{operation.id}</code> · Current status: {operation.status}
        </p>
      ))}
      <details>
        <summary>Recorded event fields</summary>
        <pre>{JSON.stringify(item.payload, null, 2)}</pre>
      </details>
    </article>
  );
}
