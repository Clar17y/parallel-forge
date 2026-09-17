import { AlertTriangle, Check, Info } from 'lucide-react';
import type { components } from '@/lib/api/schema';
import { describeRunState } from './run-presentation';

export function RunStatusBanner({ projection, stale }: { projection: components['schemas']['RunProjection']; stale: boolean }) {
  const status = describeRunState(projection);
  const Icon = status.tone === 'danger' || status.tone === 'warning' ? AlertTriangle : status.tone === 'success' ? Check : Info;
  return <section className="run-status" data-tone={stale && !projection.recovery_hold ? 'neutral' : status.tone}
    aria-label="Current run status" data-run-state={projection.run.state} role={projection.recovery_hold ? 'alert' : undefined}>
    <Icon className="run-status-icon" aria-hidden="true" />
    <div><h2>{stale ? 'Last known state: ' : ''}{status.title}</h2><p>{status.description}</p></div>
  </section>;
}
