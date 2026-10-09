'use client';

import { Panel } from '@/components/ui/panel';
import { StatusBadge } from '@/components/ui/status-badge';
import { EvidenceDetails } from './evidence-details';
import type { AuthoringOutcome } from '@/hooks/epics/types';

function formatState(state: string): string {
  return state.replaceAll('_', ' ');
}

function UsageValue({ label, value }: { label: string; value: number | null | undefined }) {
  return <li>{label}: {value === null || value === undefined ? 'Unknown' : value}</li>;
}

export function AuthoringJobOutcome({
  outcome,
  title = 'Assistant job',
}: {
  outcome: AuthoringOutcome;
  title?: string;
}) {
  const usage = outcome.usage;
  const unknown = outcome.unknown_usage_fields ?? [];
  const held = outcome.held_reservations;
  const settlementLabel = outcome.process_settled
    ? 'Process settled'
    : outcome.state === 'cancelled'
      ? 'No process settlement reported'
      : 'Process settlement pending';
  const settlementTone = outcome.process_settled || outcome.state === 'cancelled' ? 'neutral' : 'warning';
  return (
    <Panel title={title} description={`Job state: ${formatState(outcome.state)}`}>
      <div className="space-y-3 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge label={formatState(outcome.state)} tone={outcome.state === 'failed' ? 'danger' : outcome.state === 'proposed' ? 'success' : 'info'} />
          <StatusBadge label={settlementLabel} tone={settlementTone} />
        </div>
        {outcome.route && <p>Model: {outcome.route.model} · Effort: {outcome.route.effort}</p>}
        {outcome.failure && <p role="alert">The job reported: {formatState(outcome.failure)}.</p>}
        <p>Usage: {outcome.usage_known === true ? 'measured' : outcome.usage_known === false ? 'unknown' : 'not yet reported'}.</p>
        {usage && (
          <ul className="list-disc pl-5 space-y-1">
            <UsageValue label="Duration (ms)" value={usage.duration_ms} />
            <UsageValue label="Duration lower bound (ms)" value={usage.duration_lower_bound_ms} />
            <UsageValue label="Tool calls" value={usage.tool_call_count} />
            <UsageValue label="Input tokens" value={usage.input_tokens} />
            <UsageValue label="Output tokens" value={usage.output_tokens} />
            <UsageValue label={`Estimated cost${outcome.currency ? ` (${outcome.currency})` : ''}`} value={usage.estimated_api_cost_minor} />
          </ul>
        )}
        {unknown.length > 0 && <p>Unknown usage: {unknown.map(formatState).join(', ')}.</p>}
        {held && outcome.held_reasons && Object.values(outcome.held_reasons).some(Boolean) && (
          <EvidenceDetails summary="Inspect held usage reservations">
            <span>{JSON.stringify(held)}</span>
            <span>{JSON.stringify(outcome.held_reasons)}</span>
          </EvidenceDetails>
        )}
        <EvidenceDetails summary="Inspect job identity and version">
          <span>job_id: {outcome.job_id}</span>
          <span>job_version: {outcome.job_version}</span>
          {outcome.proposal_digest && <span>proposal_digest: {outcome.proposal_digest}</span>}
        </EvidenceDetails>
      </div>
    </Panel>
  );
}
