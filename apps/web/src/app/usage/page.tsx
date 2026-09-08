'use client';
import Link from 'next/link';
import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

export default function UsagePage() {
  const [offset, setOffset] = useState(0);
  const usage = useApi<components['schemas']['ListPage_UsageItem_']>(`/usage?offset=${offset}&limit=25`);
  return <><h1>Usage</h1><p>Recorded tokens and duration, grouped by project, run, provider, model and currency. Costs are estimates; unpriced calls are excluded from known cost.</p>
    <button onClick={usage.refresh} disabled={usage.loading}>Refresh usage</button>
    {usage.loading && <p role="status">Loading usage…</p>}
    {usage.failed && <p role="alert">Usage unavailable. <button onClick={usage.refresh}>Retry</button></p>}
    {usage.value && <>{!usage.value.items.length && <p>No usage on this page.</p>}
      {usage.value.items.map(item => <article key={`${item.project_id}:${item.run_id}:${item.provider}:${item.model}:${item.currency}`}>
        <h2>{item.provider} / {item.model}</h2><p><Link href={`/projects/${item.project_id}`}>Project {item.project_id}</Link> · <Link href={`/runs/${item.run_id}`}>Run {item.run_id}</Link></p>
        <dl><dt>Input / output tokens</dt><dd>{item.input_tokens} / {item.output_tokens}</dd>
          <dt>Duration</dt><dd>{item.duration_ms} ms</dd><dt>Model calls</dt><dd>{item.model_calls}</dd>
          <dt>Known estimated cost</dt><dd>{item.known_cost_minor} {item.currency} minor units</dd></dl>
        {item.unpriced_calls > 0 && <p>{item.unpriced_calls} {item.unpriced_calls === 1 ? 'call' : 'calls'} with unknown cost</p>}
      </article>)}</>}
    <nav aria-label="Usage pages"><button disabled={offset === 0 || usage.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</button>
      <button disabled={!usage.value?.truncated || usage.loading} onClick={() => setOffset(offset + 25)}>Next</button></nav>
  </>;
}
