'use client';
import { LoadingStatus } from '@/components/ui/loading-status';
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
      {history.loading && <LoadingStatus>Loading check history…</LoadingStatus>}
      {history.failed && <p role="alert">Check history unavailable. <button onClick={history.refresh}>Retry</button></p>}
      {history.value && !history.value.items.length && (
        <div className="empty-state-notice">
          <p>No local validation checks recorded.</p>
          <p className="meta">Validation commands will run once implementation is ready for verification.</p>
        </div>
      )}
      {history.value?.items.map(check => <article key={check.id}><h4>{check.name}</h4>
        <p>Attempt {check.attempt ?? 'unavailable'} · Duration: {check.duration_ms === null ? (['PENDING', 'RUNNING'].includes(check.status.toUpperCase()) ? 'Pending' : 'Unknown') : `${check.duration_ms} ms`}</p>
        {check.source === 'subscription' && <p>Subscription task <code>{check.subscription_task_id}</code> · Attempt <code>{check.subscription_attempt_id}</code></p>}
        <p>Configured runner: {check.configured_runner_mode}</p>
        {check.evidence_digest && <p><a href={`/api/artifacts/${check.evidence_digest}/download`}>{check.source === 'subscription' ? 'Check receipt' : 'Validation evidence'} for attempt {check.attempt}</a></p>}
        <p>{check.command_name} · version {check.command_version} · {check.status}</p>
        <p>Head: <code>{check.head_sha ?? 'Unavailable'}</code> · Exit code: {check.exit_code ?? 'Not recorded'}</p>
        {check.output_artifact_digest && <a href={`/api/artifacts/${check.output_artifact_digest}/download`}>Bounded output for {check.name}</a>}
        {check.stderr_artifact_digest && <p><a href={`/api/artifacts/${check.stderr_artifact_digest}/download`}>Standard error for {check.name}</a></p>}
      </article>)}
      <nav aria-label="Check history pages"><button disabled={offset === 0 || history.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous checks</button>
        <button disabled={!history.value?.truncated || history.loading} onClick={() => setOffset(offset + 25)}>Next checks</button></nav>
    </section>
    <section><h3>GitHub checks</h3>
      {!remote ? (
        <div className="empty-state-notice">
          <p>No remote observation recorded.</p>
          <p className="meta">Remote GitHub checks will be monitored after a pull request is published.</p>
        </div>
      ) : <><ObservationEvidence observation={remote} />
        {!remote.checks.length && <p>No checks returned in this observation.</p>}
        {remote.checks.map((check, index) => <article key={`${check.name}:${index}`}><h4>{check.name}</h4>
          <p>{check.status} · <span>{check.conclusion ?? 'Pending'}</span></p>
          <p>Check head: <code>{check.head_sha ?? 'Unavailable'}</code></p>
          {check.summary && <p>{check.summary}</p>}{check.text && <details><summary>Check details</summary><pre>{check.text}</pre></details>}
        </article>)}</>}
    </section>
  </section>;
}
