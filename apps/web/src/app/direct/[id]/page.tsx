'use client';
import { useState } from 'react';
import { useParams } from 'next/navigation';
import { LoadingStatus } from '@/components/ui/loading-status';
import { useApi } from '@/hooks/use-api';
import { mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';

type Run = components['schemas']['DirectRunResponse'];
const TERMINAL = new Set(['DONE', 'FAILED', 'CANCELLED', 'NEEDS_DECISION']);

export default function DirectDetailPage() {
  const { id } = useParams<{ id: string }>();
  const run = useApi<Run>(`/direct-runs/${encodeURIComponent(id)}`);
  const terminal = run.value ? TERMINAL.has(run.value.state) : false;
  const transcript = useApi<components['schemas']['DirectTranscriptResponse']>(
    `/direct-runs/${encodeURIComponent(id)}/transcript`,
    { refreshIntervalMs: terminal ? undefined : 3000 },
  );
  const diff = useApi<components['schemas']['DirectDiffResponse']>(
    terminal ? `/direct-runs/${encodeURIComponent(id)}/diff` : null,
  );
  const [error, setError] = useState('');
  async function cancel() {
    try {
      await mutate(`/direct-runs/${encodeURIComponent(id)}/cancel`, {}, { idempotencyKey: crypto.randomUUID() });
      run.refresh();
    } catch { setError('Cancel failed.'); }
  }
  if (run.loading) return <LoadingStatus>Loading direct run…</LoadingStatus>;
  if (run.failed || !run.value) return <p role="alert">Direct run unavailable.</p>;
  const value = run.value;
  return <>
    <h1>Direct run</h1>
    <p>State: {value.state}</p>
    <p>{value.task}</p>
    {value.summary && <p>{value.summary}</p>}
    {value.state === 'NEEDS_DECISION' && value.question && <section><h2>Decision needed</h2><p>{value.question}</p><ul>{value.options.map(o => <li key={o}>{o}</li>)}</ul></section>}
    {value.failure_reason && <p role="alert">{value.failure_reason}</p>}
    {!terminal && <button onClick={cancel}>Cancel run</button>}
    {error && <p role="alert">{error}</p>}
    <h2>Checks</h2>
    {(value.checks ?? []).length === 0 && <p>No checks recorded yet.</p>}
    <ul>{(value.checks ?? []).map(check => (
      <li key={check.id}>
        <span>Round {check.round}: {check.command_name} ({check.kind}) — {check.status}</span>
        {check.failure_excerpt && <pre>{check.failure_excerpt}</pre>}
      </li>
    ))}</ul>
    <h2>Reviews</h2>
    {(value.reviews ?? []).length === 0 && <p>No reviews recorded yet.</p>}
    <ul>{(value.reviews ?? []).map(review => (
      <li key={review.id}>
        <span>Round {review.round}: {review.client}/{review.model} — {review.verdict}</span>
        {review.summary && <p>{review.summary}</p>}
        {review.reviewer_modified_worktree && <p>Reviewer modified worktree (reverted)</p>}
        <ul>{(review.findings ?? []).map((finding, i) => (
          <li key={i}>[{finding.severity}]{finding.file ? ` ${finding.file}:` : ''} {finding.detail}</li>
        ))}</ul>
      </li>
    ))}</ul>
    <h2>Transcript</h2>
    <pre>{transcript.value?.text ?? ''}</pre>
    {terminal && <><h2>Diff</h2><pre>{diff.value?.diff ?? ''}</pre></>}
  </>;
}
