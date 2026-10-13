import Link from 'next/link';
import type { components } from '@/lib/api/schema';

export function ApprovalCard({ item }: { item: components['schemas']['ApprovalItem'] }) {
  const gateLabel = item.gate === 'pr' ? 'PR publication' : item.gate === 'merge' ? 'Merge' : 'Plan';
  return <article className="approval-card" data-gate={item.gate}>
    <div className="approval-card-body">
      <h2 className="approval-gate">{gateLabel} approval</h2>
      <p className="approval-task"><strong>{item.task_title}</strong> · {item.project_name}</p>
      <p className="meta">Run version {item.run_version} · Policy version {item.policy_version}</p>
      <details className="evidence-details">
        <summary>Evidence binding</summary>
        <p>Queued evidence: <code>{item.evidence_digest}</code></p>
        <p>Run ID: <code>{item.run_id}</code> · Task ID: <code>{item.task_id}</code></p>
      </details>
    </div>
    <Link className="button approval-action" data-variant="primary" href={`/runs/${item.run_id}`}>Review {item.gate} evidence</Link>
  </article>;
}
