import type { components } from '@/lib/api/schema';
import { StatusBadge } from '@/components/ui/status-badge';
import { readableLabel, type Tone } from './run-presentation';

export function AgentStatus({ agent }: { agent: components['schemas']['AgentSection'] }) {
  const usage = agent.usage;
  const state = agent.status?.toLowerCase();
  const tone: Tone = state === 'failed' ? 'danger' : state === 'running' ? 'info' : ['completed', 'succeeded'].includes(state ?? '') ? 'success' : 'neutral';
  return <details className="agent-row" aria-label={`${agent.role} execution`}>
    <summary><span className="agent-role">{agent.role}</span><StatusBadge label={agent.status ? readableLabel(agent.status) : 'Not started'} tone={tone} /></summary>
    {agent.independent && <p className="meta">Independent reviewer</p>}
    <dl className="key-values"><div><dt>Provider / model</dt><dd>{agent.provider} / {agent.model}</dd></div>
      <div><dt>Prompt version</dt><dd>{agent.instruction_version ?? 'Not yet frozen'}</dd></div>
      <div><dt>Model duration</dt><dd>{usage ? `${usage.duration_ms} ms` : 'Not yet recorded'}</dd></div>
      <div><dt>Tool calls</dt><dd>{usage?.tool_calls ?? 'Not yet recorded'}</dd></div>
      <div><dt>Recorded cost</dt><dd>{usage?.currencies.length ? usage.currencies.map(item =>
        <span key={item.currency}>{item.currency} {item.known_cost_minor} minor units{item.unpriced_calls ? `; ${item.unpriced_calls} unpriced calls` : ''}<br /></span>) : 'Not yet recorded'}</dd></div>
    </dl>
  </details>;
}
