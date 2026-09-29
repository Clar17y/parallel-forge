'use client';

import { useState } from 'react';
import type { components } from '@/lib/api/schema';
import { useApi } from '@/hooks/use-api';

type ReceiptPage = components['schemas']['RecoveryReceiptPage'];
const pageSize = 25;

export function RecoveryReceiptHistory({ runId, taskId, attemptId }: { runId: string; taskId: string; attemptId: string }) {
  const [offset, setOffset] = useState(0);
  const result = useApi<ReceiptPage>(`/runs/${runId}/subscription-tasks/${taskId}/attempts/${attemptId}/recovery/receipts?offset=${offset}&limit=${pageSize}`);
  const valid = result.value && result.value.run_id === runId && result.value.task_id === taskId
    && result.value.attempt_id === attemptId && Array.isArray(result.value.receipts)
    && result.value.receipts.length <= pageSize && typeof result.value.has_more === 'boolean'
    && result.value.receipts.every(row => row.run_id === runId && row.task_id === taskId && row.attempt_id === attemptId);
  const page = valid ? result.value : null;
  const failed = result.failed || (!!result.value && !valid);

  return <section aria-label="Recovery history" className="space-y-2 border-t border-slate-300 pt-3">
    <div className="flex flex-wrap items-center gap-3">
      <h4 className="font-semibold">Recorded recovery receipts</h4>
      <button type="button" onClick={result.refresh}>Refresh receipts</button>
    </div>
    {!page && !failed ? <p role="status">Loading recovery receipts…</p> : null}
    {failed ? <p role="alert">Recovery receipts are unavailable. Refresh to try again; this does not establish the outcome of a request.</p> : null}
    {page && page.receipts.length === 0 ? <p>No recovery receipt is recorded on this page. This does not rule out a request still in progress.</p> : null}
    {page?.receipts.map(row => <div key={row.receipt_id} className="rounded border border-slate-300 p-2">
      <p>Recovery receipt {row.receipt_id}</p>
      <p>Action: {row.action.replaceAll('_', ' ')}</p>
      <p>Status: {row.status}</p>
      <p>Recorded: {row.observed_at}</p>
      <p>Reason: {row.reason_code.replaceAll('_', ' ')}</p>
    </div>)}
    <nav aria-label="Recovery receipt pages" className="flex flex-wrap items-center gap-3">
      <button type="button" disabled={offset === 0} onClick={() => setOffset(value => Math.max(0, value - pageSize))}>Previous receipts</button>
      <span>Page {Math.floor(offset / pageSize) + 1}</span>
      <button type="button" disabled={!page?.has_more || offset > 1_000_000 - pageSize} onClick={() => setOffset(value => value + pageSize)}>Next receipts</button>
    </nav>
  </section>;
}
