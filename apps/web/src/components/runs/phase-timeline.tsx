import type { components } from '@/lib/api/schema';
import { formatDate } from '@/lib/format';

export function PhaseTimeline({ events }: { events: components['schemas']['EventItem'][] }) {
  return <section><h2>Recent activity</h2><p>Latest persisted run events.</p>
    {events.length ? <ol>{[...events].sort((a, b) => a.sequence - b.sequence).map(event => <li key={event.sequence}>
      <time dateTime={event.occurred_at}>{formatDate(event.occurred_at)}</time> · {event.event_type} · Version {event.run_version}
    </li>)}</ol> : <p>No events recorded yet.</p>}
  </section>;
}
