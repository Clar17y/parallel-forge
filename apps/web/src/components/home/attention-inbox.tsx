'use client';
import Link from 'next/link';
import { ApprovalCard } from '@/components/approvals/approval-card';
import { LoadingStatus } from '@/components/ui/loading-status';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

type ApprovalPage = components['schemas']['ListPage_ApprovalItem_'];
type RunPage = components['schemas']['RunListPage'];

/**
 * The operator's first question is "what needs me?". This inbox answers it
 * directly: decisions waiting on a human first, then runs that are blocked.
 * The server excludes pending-gate runs from the second list, so approvals
 * are never repeated there and can never starve its page.
 */
export function AttentionInbox() {
  // Poll both queues on the same cadence as the shell badge so a gate or a
  // blocked run created by a worker appears without a reload and the page
  // never contradicts the badge count.
  const approvals = useApi<ApprovalPage>('/approvals?offset=0&limit=10',
    { refreshIntervalMs: 30_000, keepPreviousOnRefresh: true });
  const runs = useApi<RunPage>('/run-projections?offset=0&limit=10&attention=true&exclude_pending_gate=true',
    { refreshIntervalMs: 30_000, keepPreviousOnRefresh: true });
  const blockedRuns = runs.value?.items ?? [];
  return <>
    <section aria-labelledby="inbox-approvals-heading">
      <h2 id="inbox-approvals-heading">Waiting for your approval</h2>
      {approvals.loading && <LoadingStatus>Loading approvals…</LoadingStatus>}
      {approvals.failed && <p role="alert">Approvals unavailable. <button onClick={approvals.refresh}>Retry approvals</button></p>}
      {approvals.value && <>
        {!approvals.value.items.length && <p>No approvals are waiting for you.</p>}
        {approvals.value.items.map(item => <ApprovalCard key={`${item.run_id}:${item.gate}:${item.evidence_digest}`} item={item} />)}
        {approvals.value.truncated && <p><Link href="/approvals">See all pending approvals</Link></p>}
      </>}
    </section>
    <section aria-labelledby="inbox-runs-heading">
      <h2 id="inbox-runs-heading">Runs needing attention</h2>
      {runs.loading && <LoadingStatus>Loading runs…</LoadingStatus>}
      {runs.failed && <p role="alert">Runs unavailable. <button onClick={runs.refresh}>Retry runs</button></p>}
      {runs.value && <>
        {!blockedRuns.length && <><p>No runs need attention.</p><p><Link href="/runs">Browse all runs</Link></p></>}
        {!!blockedRuns.length && <ul>{blockedRuns.map(item => <li key={item.run_id}>
          <Link href={`/runs/${item.run_id}`}>{item.task_title}</Link>
          <span> · {item.project_name} · {item.state.replaceAll('_', ' ')}</span>
        </li>)}</ul>}
        {runs.value.truncated && <p><Link href="/runs">See all runs</Link></p>}
      </>}
    </section>
  </>;
}
