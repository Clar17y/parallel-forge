import Link from 'next/link';
import type { components } from '@/lib/api/schema';
import { formatDate } from '@/lib/format';

export function RunList({ items }: { items: components['schemas']['RunListItem'][] }) {
  if (!items.length) return <p>No runs match these filters on this page.</p>;
  return <div className="table-scroll"><table><thead><tr>
    {['Task', 'Project', 'Phase', 'Next gate', 'Remediation', 'Elapsed', 'Estimated cost', 'Updated', 'PR'].map(label => <th key={label} scope="col">{label}</th>)}
  </tr></thead><tbody>{items.map(item => <tr key={item.run_id}>
    <td><Link href={`/runs/${item.run_id}`}>{item.task_title}</Link>{item.attention_required && <p>Attention required</p>}</td>
    <td><Link href={`/projects/${item.project_id}`}>{item.project_name}</Link></td>
    <td>{item.state.replaceAll('_', ' ')}</td><td>{item.next_gate ?? 'None'}</td>
    <td>Local {item.local_remediation_count} · Remote {item.remote_remediation_count}</td>
    <td>{Math.floor(item.elapsed_ms / 60000)}m {Math.floor(item.elapsed_ms / 1000) % 60}s</td>
    <td>{item.cost_summary.currencies.map(cost => <div key={cost.currency}>{cost.currency} {cost.known_cost_minor} minor units</div>)}
      {item.cost_summary.unpriced_calls > 0 && <div>{item.cost_summary.unpriced_calls} unpriced calls</div>}
      {!item.cost_summary.currencies.length && !item.cost_summary.unpriced_calls && 'Not yet recorded'}</td>
    <td>{formatDate(item.updated_at)}</td>
    <td>{item.pull_request ? <a target="_blank" rel="noreferrer" href={`https://github.com/${item.pull_request.repository}/pull/${item.pull_request.number}`}>#{item.pull_request.number}</a> : 'None'}</td>
  </tr>)}</tbody></table></div>;
}
