import type { components } from '@/lib/api/schema';
import { StatusBadge } from '@/components/ui/status-badge';
import { Panel } from '@/components/ui/panel';
import { Button } from '@/components/ui/button';
import { describeActivity, groupRecentActivity, safeExactTime } from '@/lib/activity-presentation';
import { RelativeTime } from './relative-time';

export function PhaseTimeline({
  events,
  onInspect,
}: {
  events: components['schemas']['EventItem'][];
  onInspect?: () => void;
}) {
  const sorted = [...events].sort((a, b) => b.sequence - a.sequence);
  const grouped = groupRecentActivity(sorted);
  const recent = grouped.slice(0, 6);
  const visibleEventCount = recent.reduce((sum, g) => sum + g.count, 0);

  return (
    <Panel
      title="Recent activity"
      description="Latest persisted run events."
      action={onInspect && <Button variant="quiet" onClick={onInspect}>View activity</Button>}
    >
      {recent.length ? (
        <ol className="activity-list">
          {recent.map(group => {
            if (group.isGroup) {
              return (
                <li key={group.key}>
                  <details>
                    <summary>
                      <RelativeTime timestamp={group.latestEvent.occurred_at} />
                      <span>{group.summaryTitle}</span>
                    </summary>
                    <div className="event-details">
                      <p className="grouped-meta">
                        {group.count} read actions between {safeExactTime(group.firstEvent.occurred_at)} and {safeExactTime(group.latestEvent.occurred_at)}
                      </p>
                      <ol className="grouped-sublist">
                        {group.items.map(event => {
                          const desc = describeActivity(event);
                          return (
                            <li key={event.sequence}>
                              <RelativeTime timestamp={event.occurred_at} />
                              <span>{desc.title}</span>
                              <code>{event.event_type}</code>
                              <span>{safeExactTime(event.occurred_at)}</span>
                              <span className="meta-text">
                                Seq {event.sequence} · v{event.run_version}
                              </span>
                              <details><summary>Recorded event fields</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>
                            </li>
                          );
                        })}
                      </ol>
                    </div>
                  </details>
                </li>
              );
            }

            const event = group.latestEvent;
            const desc = describeActivity(event);
            return (
              <li key={event.sequence}>
                <details>
                  <summary>
                    <RelativeTime timestamp={event.occurred_at} />
                    <span>{desc.title}</span>
                    {desc.outcome && <StatusBadge label={desc.outcome} tone={desc.tone} />}
                  </summary>
                  <div className="event-details">
                    <code>{event.event_type}</code>
                    {desc.outcome && <span className="outcome-text"> · {desc.outcome}</span>}
                    <p>
                      Sequence {event.sequence} · Run version {event.run_version} · Exact time: {safeExactTime(event.occurred_at)}
                    </p>
                    <details><summary>Recorded event fields</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>
                  </div>
                </details>
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="empty-state">No events recorded yet.</p>
      )}
      {events.length > visibleEventCount && (
        <p className="meta">
          Showing the latest {recent.length} {recent.length === 1 ? 'entry' : 'entries'}. Full history is in Activity.
        </p>
      )}
      {events.length === visibleEventCount && events.length > 0 && (
        <p className="meta">Showing {events.length} loaded events. Full history is in Activity.</p>
      )}
    </Panel>
  );
}
