'use client';
import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { ObservationEvidence } from './observation-evidence';

export function ChecksPanel({ projection }: { projection: components['schemas']['RunProjection'] }) {
  const remote = projection.remote_observation;
  const [offset, setOffset] = useState(0);
  const history = useApi<components['schemas']['ListPage_CheckHistoryItem_']>(`/runs/${projection.run.id}/checks?offset=${offset}&limit=25`);
  return <section aria-label="Check evidence"><h2>Checks</h2>
    <section><h3>Local validation</h3><p>Runner: {projection.security.runner_mode}</p>
      <button onClick={history.refresh} disabled={history.loading}>Refresh check history</button>
      {history.loading && <p role="status">Loading check history…</p>}
      {history.failed && <p role="alert">Check history unavailable. <button onClick={history.refresh}>Retry</button></p>}
      {history.value && !history.value.items.length && <p>No local validation checks recorded.</p>}
      {history.value?.items.map(check => <article key={check.id}><h4>{check.name}</h4>
        <p>Attempt {check.attempt ?? 'unavailable'} · Duration: {check.duration_ms === null ? 'Pending' : `${check.duration_ms} ms`}</p>
        <p>Configured runner: {check.configured_runner_mode}</p>
        {check.evidence_digest && <p><a href={`/api/artifacts/${check.evidence_digest}/download`}>Validation evidence for attempt {check.attempt}</a></p>}
        <p>{check.command_name} · version {check.command_version} · {check.status}</p>
        <p>Head: <code>{check.head_sha ?? 'Unavailable'}</code> · Exit code: {check.exit_code ?? 'Not recorded'}</p>
        {check.output_artifact_digest && <a href={`/api/artifacts/${check.output_artifact_digest}/download`}>Bounded output for {check.name}</a>}
      </article>)}
      <nav aria-label="Check history pages"><button disabled={offset === 0 || history.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous checks</button>
        <button disabled={!history.value?.truncated || history.loading} onClick={() => setOffset(offset + 25)}>Next checks</button></nav>
    </section>
    <section><h3>GitHub checks</h3>
      {!remote ? <p>No remote observation recorded.</p> : <><ObservationEvidence observation={remote} />
        {!remote.checks.length && <p>No checks returned in this observation.</p>}
        {remote.checks.map((check, index) => <article key={`${check.name}:${index}`}><h4>{check.name}</h4>
          <p>{check.status} · <span>{check.conclusion ?? 'Pending'}</span></p>
          <p>Check head: <code>{check.head_sha ?? 'Unavailable'}</code></p>
          {check.summary && <p>{check.summary}</p>}{check.text && <details><summary>Check details</summary><pre>{check.text}</pre></details>}
        </article>)}</>}
    </section>
  </section>;
}
