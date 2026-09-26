import type { components } from '@/lib/api/schema';
import { formatDate } from '@/lib/format';
import { Panel } from '@/components/ui/panel';
import { Button } from '@/components/ui/button';
import { readableLabel } from './run-presentation';

export function PhaseTimeline({ events, onInspect }: { events: components['schemas']['EventItem'][]; onInspect?: () => void }) {
  const recent = [...events].sort((a, b) => b.sequence - a.sequence).slice(0, 6);
  return <Panel title="Recent activity" description="Latest persisted run events."
    action={onInspect && <Button variant="quiet" onClick={onInspect}>View activity</Button>}>
    {recent.length ? <ol className="activity-list">{recent.map(event => <li key={event.sequence}>
      <details><summary><time dateTime={event.occurred_at}>{formatDate(event.occurred_at)}</time>
        <span>{readableLabel(event.event_type)}</span></summary>
        <div className="event-details"><code>{event.event_type}</code><p>Sequence {event.sequence} · Run version {event.run_version}</p></div>
      </details>
    </li>)}</ol> : <p className="empty-state">No events recorded yet.</p>}
    {events.length > recent.length && <p className="meta">Showing the latest {recent.length} events. Full history is in Activity.</p>}
  </Panel>;
}
