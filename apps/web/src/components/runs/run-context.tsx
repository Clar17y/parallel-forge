import type { components } from '@/lib/api/schema';
import { Panel } from '@/components/ui/panel';
import { Button } from '@/components/ui/button';
import { AgentStatus } from './agent-status';
import { AutonomyBanner } from './autonomy-banner';
import { nextGateMessage, readableLabel } from './run-presentation';

export function RunContext({ projection: p, onTasks }: { projection: components['schemas']['RunProjection']; onTasks?: () => void }) {
  return <aside className="run-context" aria-label="Run context">
    <Panel title="Next gate">
      <p>{nextGateMessage(p)}</p>
      <details className="authority-details"><summary>What Forge is authorised to do</summary><AutonomyBanner projection={p} /></details>
      {onTasks && <Button className="task-link" variant="quiet" onClick={onTasks}>Inspect task scheduling & quota</Button>}
    </Panel>
    <Panel title="Agents" description="Expand a role for execution details.">
      {Object.values(p.agents).length ? Object.values(p.agents).map(agent => <AgentStatus key={agent.role} agent={agent} />) : <p className="empty-state">No agent executions reported yet.</p>}
    </Panel>
    <Panel title="Run details">
      <dl className="key-values">
        <div><dt>State</dt><dd>{readableLabel(p.run.state)}</dd></div>
        <div><dt>Local attempts left</dt><dd>{p.budgets.local_remediation_remaining} / {p.budgets.local_remediation_limit}</dd></div>
        <div><dt>Remote attempts left</dt><dd>{p.budgets.remote_remediation_remaining} / {p.budgets.remote_remediation_limit}</dd></div>
        <div><dt>Runner</dt><dd>{p.security.runner_mode === 'trusted_host' ? 'Trusted host · unsandboxed' : p.security.runner_mode}</dd></div>
        <div><dt>Database</dt><dd>{p.resource.database_state === 'DISABLED' ? 'Not configured' : readableLabel(p.resource.database_state)}</dd></div>
      </dl>
      <details className="context-details"><summary>Repository & evidence identifiers</summary>
        <dl className="key-values">
          <div><dt>Run</dt><dd><code>{p.run.id}</code> · Version {p.run.version}</dd></div>
          <div><dt>Raw state</dt><dd><code>{p.run.state}</code></dd></div>
          <div><dt>Branch</dt><dd><code>{p.resource.branch_name ?? 'Not yet created'}</code>{p.resource.branch_removed && ' · removal recorded'}</dd></div>
          <div><dt>Worktree</dt><dd><code>{p.resource.worktree_path ?? 'Not yet created'}</code></dd></div>
          <div><dt>Database name</dt><dd><code>{p.resource.database_name ?? 'Not assigned'}</code></dd></div>
          <div><dt>Head</dt><dd><code>{p.candidate.commit ?? 'No candidate yet'}</code></dd></div>
          <div><dt>Base</dt><dd><code>{p.run.base_sha ?? 'Unavailable'}</code></dd></div>
          <div><dt>Policy version</dt><dd>{p.run.policy_version ?? 'Unavailable'}</dd></div>
        </dl>
      </details>
    </Panel>
  </aside>;
}
