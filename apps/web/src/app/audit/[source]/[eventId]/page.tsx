'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useApi } from '@/hooks/use-api';
import { LoadingStatus } from '@/components/ui/loading-status';
import { StatusBadge } from '@/components/ui/status-badge';
import { describeActivity, safeExactTime } from '@/lib/activity-presentation';
import { RelativeTime } from '@/components/runs/relative-time';
import type { components } from '@/lib/api/schema';

type AuditEvidence = components['schemas']['AuditEventEvidence'];

const UUID_REGEX = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

export default function AuditDetailEventPage() {
  const { source, eventId } = useParams<{ source: string; eventId: string }>();

  const isValidSource = source === 'run' || source === 'operator';
  const isValidId = UUID_REGEX.test(eventId ?? '');

  if (!isValidSource || !isValidId) {
    return (
      <div className="audit-detail-layout" role="alert">
        <h1>Invalid audit event parameters</h1>
        <p className="page-description">
          The requested audit source (&quot;{source}&quot;) or event ID is invalid. Source must be &quot;run&quot; or &quot;operator&quot;, and event ID must be an exact UUID.
        </p>
        <p>
          <Link href="/audit" className="button" data-variant="secondary">
            Back to Audit log
          </Link>
        </p>
      </div>
    );
  }

  return <AuditDetailLoaded source={source} eventId={eventId} />;
}

function AuditDetailLoaded({ source, eventId }: { source: 'run' | 'operator'; eventId: string }) {
  const path = source === 'run' ? `/audit/run-events/${eventId}` : `/audit/${eventId}`;
  const rawUrl = `/api${path}`;
  const evidence = useApi<AuditEvidence>(path);

  if (evidence.loading) {
    return <LoadingStatus>Loading audit event evidence…</LoadingStatus>;
  }

  if (evidence.failed || !evidence.value) {
    return (
      <div className="audit-detail-layout">
        <p role="alert">
          Audit event evidence unavailable. <button onClick={evidence.refresh}>Retry</button>
        </p>
        <p>
          <Link href="/audit">Back to Audit log</Link>
        </p>
      </div>
    );
  }

  const item = evidence.value;
  const desc = describeActivity(item);
  const sourceTone = item.source === 'operator' ? 'warning' : 'info';

  return (
    <div className="audit-detail-layout" style={{ display: 'grid', gap: '16px', maxWidth: '100%' }}>
      <nav aria-label="Breadcrumb">
        <Link href="/audit">← Back to Audit log</Link>
      </nav>

      <header className="page-header" style={{ display: 'grid', gap: '8px' }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}>
          <h1 style={{ margin: 0 }}>{desc.title}</h1>
          <a
            href={rawUrl}
            className="button"
            data-variant="secondary"
            target="_blank"
            rel="noopener noreferrer"
          >
            Raw JSON evidence
          </a>
        </div>

        <div className="audit-card-meta" style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 12px', alignItems: 'center' }}>
          <span>
            <RelativeTime timestamp={item.created_at} /> ({safeExactTime(item.created_at)})
          </span>
          <StatusBadge label={`Source: ${item.source}`} tone={sourceTone} />
          {desc.outcome && <StatusBadge label={desc.outcome} tone={desc.tone} />}
          <span>
            Actor: {item.actor_class}{item.actor_id ? ` · ${item.actor_id}` : ' · No actor ID'}
          </span>
        </div>
      </header>

      <section className="disclosure-card" style={{ padding: '14px' }}>
        <h2 style={{ fontSize: '1rem', marginTop: 0 }}>Event Context</h2>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px 16px', fontSize: '0.875rem' }}>
          <span>Event type: <code>{item.event_type}</code></span>
          <span>Event ID: <code>{item.id}</code></span>
          {item.run_id && <span><Link href={`/runs/${item.run_id}`}>Run {item.run_id}</Link></span>}
          {item.project_id && <span><Link href={`/projects/${item.project_id}`}>Project {item.project_id}</Link></span>}
          {item.subject_id && <span>Subject: {item.subject_type} (<code>{item.subject_id}</code>)</span>}
        </div>
      </section>

      <section style={{ display: 'grid', gap: '8px', minWidth: 0, maxWidth: '100%' }}>
        <h2 style={{ fontSize: '1rem', margin: 0 }}>Recorded Payload Evidence</h2>
        <div className="evidence-scroll" style={{ overflowX: 'auto', maxWidth: '100%' }}>
          <pre style={{ margin: 0 }}>{JSON.stringify(item.payload, null, 2)}</pre>
        </div>
      </section>
    </div>
  );
}
