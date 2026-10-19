'use client';

import Link from 'next/link';
import { useState } from 'react';
import { LoadingStatus } from '@/components/ui/loading-status';
import { ApprovalCard } from '@/components/approvals/approval-card';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

export default function ApprovalsPage() {
  const [offset, setOffset] = useState(0);
  const approvals = useApi<components['schemas']['ListPage_ApprovalItem_']>(`/approvals?offset=${offset}&limit=25`);

  return (
    <div className="approvals-layout">
      <header className="page-header">
        <h1>Approvals</h1>
        <p className="page-description">
          Open a run to refresh and review its exact evidence before approving.
        </p>
      </header>

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '16px' }}>
        <button onClick={approvals.refresh} disabled={approvals.loading}>Refresh approvals</button>
      </div>

      {approvals.loading && <LoadingStatus>Loading approvals…</LoadingStatus>}
      {approvals.failed && (
        <p role="alert">
          Approvals unavailable. <button onClick={approvals.refresh}>Retry</button>
        </p>
      )}

      {approvals.value && (
        <>
          {!approvals.value.items.length && (
            <div className="empty-state" style={{ padding: '24px', border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)' }}>
              <h2>All caught up</h2>
              <p>No pending approvals are waiting for human intervention on this page.</p>
              <div style={{ display: 'flex', gap: '8px', marginTop: '12px' }}>
                <Link href="/runs" className="button" data-variant="secondary">Browse all runs</Link>
                <Link href="/audit" className="button" data-variant="quiet">Inspect audit log</Link>
              </div>
            </div>
          )}

          {approvals.value.items.length > 0 && (
            <div className="card-stack">
              {approvals.value.items.map(item => (
                <ApprovalCard key={`${item.run_id}:${item.gate}:${item.evidence_digest}`} item={item} />
              ))}
            </div>
          )}
        </>
      )}

      {(offset > 0 || (approvals.value?.items && approvals.value.items.length > 0)) && (
        <nav aria-label="Approval pages" className="pagination-bar" style={{ display: 'flex', gap: '8px', marginTop: '16px' }}>
          <button disabled={offset === 0 || approvals.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</button>
          <button disabled={!approvals.value?.truncated || approvals.loading} onClick={() => setOffset(offset + 25)}>Next</button>
        </nav>
      )}
    </div>
  );
}
