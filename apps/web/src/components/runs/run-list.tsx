import Link from 'next/link';
import { Activity } from 'lucide-react';
import type { components } from '@/lib/api/schema';
import { formatDate } from '@/lib/format';
import { isActiveRunPhase, runStateTitle, runStateTone } from './run-presentation';

export function formatElapsed(ms: number): string {
  if (ms <= 0 || !Number.isFinite(ms)) return '0s';
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  if (days > 0) {
    const remHours = hours % 24;
    return remHours > 0 ? `${days}d ${remHours}h` : `${days}d`;
  }
  if (hours > 0) {
    const remMin = minutes % 60;
    return remMin > 0 ? `${hours}h ${remMin}m` : `${hours}h`;
  }
  if (minutes > 0) {
    const remSec = seconds % 60;
    return `${minutes}m ${remSec}s`;
  }
  return `${seconds}s`;
}

export function RunList({ items }: { items: components['schemas']['RunListItem'][] }) {
  if (!items.length) return <p>No runs match these filters on this page.</p>;
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            {['Task', 'Project', 'Phase', 'Next gate', 'Remediation', 'Elapsed', 'Estimated cost', 'Updated', 'PR'].map(label => (
              <th key={label} scope="col">{label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map(item => {
            const isActivePhase = isActiveRunPhase(item.state);
            const isCancelled = item.state === 'CANCELLED';
            const isCompleted = item.state === 'COMPLETED';
            const hasRemediation = item.local_remediation_count > 0 || item.remote_remediation_count > 0;
            return (
              <tr key={item.run_id} data-run-state={item.state}>
                <td className="run-task-cell">
                  <Link href={`/runs/${item.run_id}`} className="font-medium [overflow-wrap:anywhere]">
                    {item.task_title}
                  </Link>
                  {item.attention_required && <p className="attention-flag">Attention required</p>}
                </td>
                <td><Link href={`/projects/${item.project_id}`}>{item.project_name}</Link></td>
                <td>
                  <span className="state-pill" data-tone={runStateTone(item.state)}>
                    {isActivePhase ? <Activity className="w-3.5 h-3.5 flex-none" aria-hidden="true" /> : null}
                    {isCancelled ? 'Run cancelled' : isCompleted ? 'Run completed' : runStateTitle(item.state)}
                  </span>
                </td>
                <td>{item.next_gate ?? <span className="text-muted">—</span>}</td>
                <td>{hasRemediation ? `Local ${item.local_remediation_count} · Remote ${item.remote_remediation_count}` : <span className="text-muted">—</span>}</td>
                <td title={`${Math.floor(item.elapsed_ms / 1000)}s total`}>{formatElapsed(item.elapsed_ms)}</td>
                <td>
                  {item.cost_summary.currencies.map(cost => (
                    <div key={cost.currency}>{cost.currency} {cost.known_cost_minor} minor units</div>
                  ))}
                  {item.cost_summary.unpriced_calls > 0 && <div>{item.cost_summary.unpriced_calls} unpriced calls</div>}
                  {!item.cost_summary.currencies.length && !item.cost_summary.unpriced_calls && 'Not yet recorded'}
                </td>
                <td>{formatDate(item.updated_at)}</td>
                <td>
                  {item.pull_request ? (
                    <a target="_blank" rel="noreferrer" href={`https://github.com/${item.pull_request.repository}/pull/${item.pull_request.number}`}>
                      #{item.pull_request.number}
                    </a>
                  ) : <span className="text-muted">—</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
