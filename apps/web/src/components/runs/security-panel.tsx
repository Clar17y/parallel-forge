'use client';
import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

export function SecurityPanel({ projection }: { projection: components['schemas']['RunProjection'] }) {
  const [offset, setOffset] = useState(0);
  const approvals = useApi<components['schemas']['ListPage_ApprovalHistoryItem_']>(`/runs/${encodeURIComponent(projection.run.id)}/approval-history?offset=${offset}&limit=25`);
  const security = projection.security;
  const docker = security.runner_mode === 'docker';
  return <section aria-label="Run security"><h2>Security</h2>
    <p>Frozen policy version {projection.run.policy_version} · <code>{projection.project.policy_digest}</code></p>
    <h3>Command runner policy</h3>
    <p>{docker ? 'Docker' : 'Trusted host · unsandboxed'}</p>
    <p>{docker ? 'Only the canonical managed worktree is mounted. Commands run as UID/GID 10001 without a Docker socket. Network is disabled unless the named command allows it.' : 'Commands execute on the operator-designated trusted host. Container mount and network isolation do not apply.'}</p>
    <p>Managed worktree: <code>{projection.resource.worktree_path ?? 'Not yet created'}</code></p>
    <p>These are configured controls. Check execution evidence for the actual runner used.</p>
    {security.commands.length ? <ul>{security.commands.map(command => <li key={command.name}>{command.name}: network {command.network_enabled ? 'allowed' : 'disabled'}</li>)}</ul> : <p>No named commands configured.</p>}
    {!docker && <p>Network flags do not enforce isolation in trusted-host mode.</p>}
    <h3>Protected paths</h3><p>File contents are excluded from agent repository reads. These protections do not prevent trusted project code from reading files available to its command process.</p>
    <ul>{security.secret_paths.map(path => <li key={path}><code>{path}</code></li>)}</ul>
    <h3>Providers and remote credentials</h3>
    <ul>{Object.values(projection.agents).map(agent => <li key={agent.role}>{agent.role}: {agent.provider} / {agent.model}</li>)}</ul>
    <p>Selected task and repository context is sent to the configured model provider. Provider and GitHub credentials stay behind worker-owned interfaces; runtime agents receive named tools, not raw credentials. Project commands can access only their allowlisted environment values.</p>
    <h3>Residual risks</h3><p>Repository content and command output are untrusted. Container isolation and output redaction reduce exposure but do not make arbitrary project code trustworthy. Review evidence before authorizing remote publication or merge.</p>
    <h3>Approval history</h3><p>Records describe past decisions and their current invalidation status. Available actions come from the current run controls.</p>
    <button disabled={approvals.loading} onClick={approvals.refresh}>Refresh approvals</button>
    {approvals.loading && <p role="status">Loading approval history…</p>}
    {approvals.failed && <p role="alert">Approval history unavailable. <button onClick={approvals.refresh}>Retry</button></p>}
    {approvals.value && <>{!approvals.value.items.length && <p>No approvals recorded on this page.</p>}
      {approvals.value.items.map(item => <article key={item.id}><h4>{item.gate} approval</h4>
        <p>Operator: {item.authenticated_actor_id} · <time dateTime={item.created_at}>{item.created_at}</time></p>
        <p>Run version {item.run_version} · Policy version {item.policy_version}</p>
        <p>{item.invalidated_at ? `Invalidated ${item.invalidated_at} · ${item.invalidation_reason ?? 'Reason unavailable'}` : 'No invalidation recorded'}</p>
        <a href={`/api/artifacts/${item.evidence_digest}/download`}>Approved evidence</a> · <code>{item.evidence_digest}</code>
      </article>)}</>}
    <nav aria-label="Approval history pages"><button disabled={offset === 0 || approvals.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Newer approvals</button>
      <button disabled={!approvals.value?.truncated || approvals.loading} onClick={() => setOffset(offset + 25)}>Older approvals</button></nav>
  </section>;
}
