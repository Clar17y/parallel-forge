import Link from 'next/link';
import type { components } from '@/lib/api/schema';

export function ApprovalCard({ item }: { item: components['schemas']['ApprovalItem'] }) {
  const gateLabel = item.gate === 'pr' ? 'PR publication' : item.gate === 'merge' ? 'Merge' : 'Plan';
  return <article>
    <h2>{gateLabel} approval</h2>
    <p><strong>{item.task_title}</strong> · {item.project_name}</p>
    <p>Run version {item.run_version} · Policy version {item.policy_version}</p>
    <details>
      <summary>Evidence binding</summary>
      <p>Queued evidence: <code>{item.evidence_digest}</code></p>
      <p>Run ID: <code>{item.run_id}</code> · Task ID: <code>{item.task_id}</code></p>
    </details>
    <Link href={`/runs/${item.run_id}`}>Review {item.gate} evidence</Link>
  </article>;
}
