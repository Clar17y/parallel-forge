import type { components } from '@/lib/api/schema';
import { Panel } from '@/components/ui/panel';
import { Button } from '@/components/ui/button';
import { StatusBadge } from '@/components/ui/status-badge';
import { localCheckDisplay, remoteCheckDisplay } from './run-presentation';

export function ChecksSummary({ projection, stale, onInspect }: {
  projection: components['schemas']['RunProjection']; stale: boolean; onInspect: () => void;
}) {
  const remote = projection.remote_observation;
  return <Panel title="Checks at a glance" description="Recorded results, not a merge-readiness verdict."
    action={<Button variant="quiet" onClick={onInspect}>View checks</Button>}>
    {stale && <p className="meta">Last known evidence. Refresh and reconnect before relying on these results.</p>}
    <div className="check-group"><h3>Local validation</h3>
      {!projection.checks.length ? <p className="empty-state">No local validation checks recorded.</p> : <ul className="check-list">
        {projection.checks.slice(0, 6).map(check => {
          const status = localCheckDisplay(check, projection.candidate.commit);
          return <li key={check.id}><span>{check.name}
            {status.tone === 'neutral' && <span className="check-detail">Recorded: {check.status} · Head: {check.head_sha?.slice(0, 7) ?? 'unknown'}</span>}
          </span><StatusBadge {...status} label={stale ? `Last known: ${status.label}` : status.label} tone={stale ? 'neutral' : status.tone} /></li>;
        })}
      </ul>}
      {projection.checks.length > 6 && <p className="meta">Showing 6 of {projection.checks.length} recorded checks. View checks for the full evidence.</p>}
    </div>
    <div className="check-group"><h3>GitHub observation</h3>
      {!remote ? <p className="empty-state">No remote observation recorded.</p> : !remote.checks.length ? <p className="empty-state">No checks returned in this observation.</p> : <ul className="check-list">
        {remote.checks.slice(0, 6).map((check, index) => {
          const status = remoteCheckDisplay(check, projection.candidate.commit, remote.head_sha);
          return <li key={`${check.name}:${index}`}><span>{check.name}
            {status.tone === 'neutral' && <span className="check-detail">Recorded: {check.conclusion ?? check.status} · Head: {check.head_sha?.slice(0, 7) ?? 'unknown'}</span>}
          </span><StatusBadge {...status} label={stale ? `Last known: ${status.label}` : status.label} tone={stale ? 'neutral' : status.tone} /></li>;
        })}
      </ul>}
      {!!remote && remote.checks.length > 6 && <p className="meta">Showing 6 of {remote.checks.length} observed checks. View checks for the full evidence.</p>}
    </div>
  </Panel>;
}
