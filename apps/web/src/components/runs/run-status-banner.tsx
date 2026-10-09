import { AlertTriangle, Check, Info, Loader2 } from 'lucide-react';
import type { components } from '@/lib/api/schema';
import { describeRunState } from './run-presentation';

const executingStates: components['schemas']['RunState'][] = [
  'PLANNING',
  'PREPARING_WORKTREE',
  'IMPLEMENTING',
  'VALIDATING',
  'REVIEWING',
  'REMEDIATING',
  'PUBLISHING_PR',
  'MERGING',
];

export function RunStatusBanner({
  projection,
  stale,
}: {
  projection: components['schemas']['RunProjection'];
  stale: boolean;
}) {
  const status = describeRunState(projection);
  const isExecuting =
    !stale &&
    !projection.recovery_hold &&
    projection.run.state !== 'PAUSED' &&
    executingStates.includes(projection.run.state);

  const Icon =
    status.tone === 'danger' || status.tone === 'warning'
      ? AlertTriangle
      : status.tone === 'success'
        ? Check
        : Info;

  return (
    <section
      className="run-status"
      data-tone={stale && !projection.recovery_hold ? 'neutral' : status.tone}
      aria-label="Current run status"
      data-run-state={projection.run.state}
      role={projection.recovery_hold ? 'alert' : undefined}
    >
      {isExecuting ? (
        <Loader2 className="run-status-icon animate-spin motion-reduce:animate-none" aria-hidden="true" />
      ) : (
        <Icon className="run-status-icon" aria-hidden="true" />
      )}
      <div>
        <h2>
          {stale ? 'Last known state: ' : ''}
          {status.title}
        </h2>
        <p>{status.description}</p>
      </div>
    </section>
  );
}
