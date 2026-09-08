import Link from 'next/link';
import type { components } from '@/lib/api/schema';

export function ApprovalCard({ item }: { item: components['schemas']['ApprovalItem'] }) {
  return <article>
    <h2>{item.gate === 'pr' ? 'PR publication' : item.gate === 'merge' ? 'Merge' : 'Plan'} approval</h2>
    <p>Run {item.run_id} · Task {item.task_id}</p>
    <p>Run version {item.run_version} · Policy version {item.policy_version}</p>
    <p>Queued evidence: <code>{item.evidence_digest}</code></p>
    <Link href={`/runs/${item.run_id}`}>Review {item.gate} evidence</Link>
  </article>;
}
