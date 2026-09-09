'use client';
import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

export default function EvaluationsPage() {
  const [offset, setOffset] = useState(0);
  const evaluations = useApi<components['schemas']['ListPage_EvaluationItem_']>(`/evaluations?offset=${offset}&limit=25`);
  return <><h1>Evaluations</h1><p>Persisted fixture results and model usage. Pending or missing results do not establish a pass.</p>
    <button onClick={evaluations.refresh} disabled={evaluations.loading}>Refresh evaluations</button>
    {evaluations.loading && <p role="status">Loading evaluations…</p>}
    {evaluations.failed && <p role="alert">Evaluations unavailable. <button onClick={evaluations.refresh}>Retry</button></p>}
    {evaluations.value && <>{!evaluations.value.items.length && <p>No evaluations on this page.</p>}
      {evaluations.value.items.map(item => <article key={`${item.suite_id}:${item.case_id ?? 'suite'}`}>
        <h2>{item.case_key ?? 'Suite without recorded cases'}</h2><p>Suite {item.suite_id} · {item.suite_status}</p>
        <p>Fixture {item.fixture_version} · Metrics {item.metric_version} · {item.role ?? 'Role unavailable'}</p>
        <p>{item.status ?? 'No case result'}</p>
        <p>Prompt version: {item.prompt_version ?? 'Unavailable'}</p>
        {item.provider === null || item.model === null ? <p>Model usage unavailable</p> : <>
          <p>{item.provider} / {item.model}</p><p>Input / output tokens: {item.input_tokens} / {item.output_tokens} · Duration: {item.duration_ms} ms</p>
          <p>Estimated cost: {item.estimated_cost_minor === null ? 'Unknown' : `${item.estimated_cost_minor} ${item.currency} minor units`}</p></>}
        {item.metrics && <details><summary>Recorded metrics</summary><pre>{JSON.stringify(item.metrics, null, 2)}</pre></details>}
        {item.input_artifact_digest && <p><a href={`/api/artifacts/${item.input_artifact_digest}/download`}>Input evidence</a> · <code>{item.input_artifact_digest}</code></p>}
        {item.output_artifact_digest && <p><a href={`/api/artifacts/${item.output_artifact_digest}/download`}>Output evidence</a> · <code>{item.output_artifact_digest}</code></p>}
      </article>)}</>}
    <nav aria-label="Evaluation pages"><button disabled={offset === 0 || evaluations.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</button>
      <button disabled={!evaluations.value?.truncated || evaluations.loading} onClick={() => setOffset(offset + 25)}>Next</button></nav>
  </>;
}
