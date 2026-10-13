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
 * Runs already shown as pending approvals are not repeated in the second list.
 */
export function AttentionInbox() {
  const approvals = useApi<ApprovalPage>('/approvals?offset=0&limit=10');
  const runs = useApi<RunPage>('/run-projections?offset=0&limit=10&attention=true');
  const approvalRunIds = new Set(approvals.value?.items.map(item => item.run_id) ?? []);
  const blockedRuns = runs.value?.items.filter(item => !approvalRunIds.has(item.run_id)) ?? [];
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
