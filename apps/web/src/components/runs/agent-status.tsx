import type { components } from '@/lib/api/schema';

export function AgentStatus({ agent }: { agent: components['schemas']['AgentSection'] }) {
  const usage = agent.usage;
  return <section aria-label={`${agent.role} execution`}><h3>{agent.role}{agent.independent ? ' · Independent reviewer' : ''}</h3>
    <dl><dt>Provider / model</dt><dd>{agent.provider} / {agent.model}</dd>
      <dt>Status</dt><dd>{agent.status ?? 'Not started'}</dd>
      <dt>Prompt version</dt><dd>{agent.instruction_version ?? 'Not yet frozen'}</dd>
      <dt>Model duration</dt><dd>{usage ? `${usage.duration_ms} ms` : 'Not yet recorded'}</dd>
      <dt>Tool calls</dt><dd>{usage?.tool_calls ?? 'Not yet recorded'}</dd>
      <dt>Recorded cost</dt><dd>{usage?.currencies.length ? usage.currencies.map(item =>
        <span key={item.currency}>{item.currency} {item.known_cost_minor} minor units{item.unpriced_calls ? `; ${item.unpriced_calls} unpriced calls` : ''}<br /></span>) : 'Not yet recorded'}</dd>
    </dl>
  </section>;
}
