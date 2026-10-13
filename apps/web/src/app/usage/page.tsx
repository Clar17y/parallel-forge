'use client';
import { LoadingStatus } from '@/components/ui/loading-status';
import Link from 'next/link';
import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import { SubscriptionUsageSummary } from '@/components/subscription-usage/usage-summary';
import { getProviderCue } from '@/components/ui/provider-badge';
import { HistoricalUsageReport } from '@/components/task-usage/historical-usage-report';
import type { components } from '@/lib/api/schema';

export default function UsagePage() {
  const [offset, setOffset] = useState(0);
  const usage = useApi<components['schemas']['ListPage_UsageItem_']>(`/usage?offset=${offset}&limit=25`);
  return <><h1>Usage</h1><HistoricalUsageReport /><SubscriptionUsageSummary /><section className="api-usage" aria-labelledby="api-usage-heading">
    <div className="section-heading"><div><h2 id="api-usage-heading">API usage</h2><p className="meta">Recorded tokens and duration, grouped by project, run, provider, model and currency. Costs are estimates; unpriced calls are excluded from known cost.</p></div>
    <button onClick={usage.refresh} disabled={usage.loading}>Refresh usage</button></div>
    {usage.loading && <LoadingStatus>Loading usage…</LoadingStatus>}
    {usage.failed && <p role="alert">Usage unavailable. <button onClick={usage.refresh}>Retry</button></p>}
    {usage.value && <>{!usage.value.items.length && <p>No usage on this page.</p>}
      <div className="usage-grid">{usage.value.items.map(item => <article className="usage-card" key={`${item.project_id}:${item.run_id}:${item.provider}:${item.model}:${item.currency}`}>
        <h3 className="usage-title" data-provider={getProviderCue(item.provider).tone}><span className="provider-badge-indicator" aria-hidden="true" />{item.provider} / {item.model}</h3><p className="meta"><Link href={`/projects/${item.project_id}`}>Project {item.project_id}</Link> · <Link href={`/runs/${item.run_id}`}>Run {item.run_id}</Link></p>
        <dl className="key-values"><div><dt>Input / output tokens</dt><dd>{item.input_tokens} / {item.output_tokens}</dd></div>
          <div><dt>Duration</dt><dd>{item.duration_ms} ms</dd></div><div><dt>Model calls</dt><dd>{item.model_calls}</dd></div>
          <div><dt>Known estimated cost</dt><dd>{item.known_cost_minor} {item.currency} minor units</dd></div></dl>
        {item.unpriced_calls > 0 && <p className="meta">{item.unpriced_calls} {item.unpriced_calls === 1 ? 'call' : 'calls'} with unknown cost</p>}
      </article>)}</div></>}
    <nav aria-label="Usage pages"><button disabled={offset === 0 || usage.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</button>
      <button disabled={!usage.value?.truncated || usage.loading} onClick={() => setOffset(offset + 25)}>Next</button></nav>
  </section></>;
}
