'use client';
import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

export function UsagePanel({ runId }: { runId: string }) {
  const [offset, setOffset] = useState(0);
  const usage = useApi<components['schemas']['ListPage_RunUsageItem_']>(`/runs/${encodeURIComponent(runId)}/usage?offset=${offset}&limit=25`);
  return <section aria-label="Run usage"><h2>Usage</h2>
    <p>Recorded token counts and estimated costs for this run, newest first. Calls without a recorded estimate remain unknown.</p>
    <button disabled={usage.loading} onClick={usage.refresh}>Refresh usage</button>
    {usage.loading && <p role="status">Loading usage…</p>}
    {usage.failed && <p role="alert">Usage unavailable. <button onClick={usage.refresh}>Retry</button></p>}
    {usage.value && <>{!usage.value.items.length && <p>No usage recorded on this page.</p>}
      {usage.value.items.map(item => <article key={item.id}>
        <h3>{item.role} · {item.provider} / {item.model}</h3>
        <p><time dateTime={item.created_at}>{item.created_at}</time> · Execution: <code>{item.agent_execution_id}</code></p>
        <p>Prompt version: {item.prompt_version}</p>
        <p>{item.instruction_digest ? <>Historical prompt digest: <code>{item.instruction_digest}</code></> : 'Historical prompt digest unavailable'}</p>
        <h4>Actual tokens</h4><p>{item.input_tokens} input · {item.output_tokens} output · {item.cached_input_tokens} cached input</p>
        <h4>Estimated cost</h4>
        <p>{item.estimated_cost_minor === null ? `Unknown estimate · ${item.currency} · ${item.unknown_price_reason ?? 'No pricing evidence'}` : `${item.estimated_cost_minor} minor units · ${item.currency}`}</p>
        <p>Pricing version: {item.pricing_version}</p><p>{item.duration_ms} ms · {item.tool_call_count} tool calls</p>
      </article>)}</>}
    <nav aria-label="Usage pages"><button disabled={offset === 0 || usage.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Newer usage</button>
      <button disabled={!usage.value?.truncated || usage.loading} onClick={() => setOffset(offset + 25)}>Older usage</button></nav>
  </section>;
}
