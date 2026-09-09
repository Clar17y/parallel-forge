'use client';
import { useState } from 'react';
import { ApprovalCard } from '@/components/approvals/approval-card';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

export default function ApprovalsPage() {
  const [offset, setOffset] = useState(0);
  const approvals = useApi<components['schemas']['ListPage_ApprovalItem_']>(`/approvals?offset=${offset}&limit=25`);
  return <><h1>Approvals</h1>
    <p>Open a run to refresh and review its exact evidence before approving.</p>
    <button onClick={approvals.refresh} disabled={approvals.loading}>Refresh approvals</button>
    {approvals.loading && <p role="status">Loading approvals…</p>}
    {approvals.failed && <p role="alert">Approvals unavailable. <button onClick={approvals.refresh}>Retry</button></p>}
    {approvals.value && <>
      {!approvals.value.items.length && <p>No pending approvals on this page.</p>}
      {approvals.value.items.map(item => <ApprovalCard key={`${item.run_id}:${item.gate}:${item.evidence_digest}`} item={item} />)}
    </>}
    <nav aria-label="Approval pages">
      <button disabled={offset === 0 || approvals.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</button>
      <button disabled={!approvals.value?.truncated || approvals.loading} onClick={() => setOffset(offset + 25)}>Next</button>
    </nav>
  </>;
}
