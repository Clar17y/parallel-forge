'use client';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { api, ApiError, mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';
import { ResourceTeardown } from './resource-teardown';
import { parseMergeEvidence, MergeApprovalEvidence, type MergeEvidence } from './merge-approval-evidence';
import { parsePrEvidence, PrPublicationEvidence, type PrEvidence } from './pr-publication-evidence';

type Projection = components['schemas']['RunProjection'];
type Command = components['schemas']['AvailableCommand'];
type Binding = { resource?: Projection['resource']; command: Command; key: string; evidence?: Record<string, unknown>;
  merge?: MergeEvidence; protection?: components['schemas']['ProtectionSnapshotResponse']; pr?: PrEvidence; body?: string; challenge?: components['schemas']['ApprovalChallengeResponse']; payload?: Record<string, unknown> };
const labels: Record<string, string> = { teardown_run_resources: 'Remove run resources', pause: 'Pause', resume: 'Resume', cancel: 'Cancel run',
  request_plan_revision: 'Request revision', approve_plan: 'Approve plan', approve_pr: 'Approve PR publication', approve_merge: 'Approve merge' };

export function RunControls({ projection, onRefresh, disabled = false }: {
  projection: Projection; onRefresh: () => Promise<Projection>; disabled?: boolean;
}) {
  const [binding, setBinding] = useState<Binding | null>(null);
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [message, setMessage] = useState('');
  const busy = useRef(false);
  const lifecycle = useRef<AbortController | null>(null);
  useEffect(() => { const owner = new AbortController(); lifecycle.current = owner; return () => owner.abort(); }, []);
  const current = binding && projection.available_commands.find(item => item.name === binding.command.name);
  const stale = !!binding && (!current || projection.run.version !== binding.command.expected_run_version || current.expected_run_version !== binding.command.expected_run_version || current.evidence_digest !== binding.command.evidence_digest || current.policy_version !== binding.command.policy_version || (!!binding.resource && projection.resource.teardown_confirmation !== binding.resource.teardown_confirmation));

  async function report(error: unknown, signal: AbortSignal) {
    if (signal.aborted) return;
    if (error instanceof ApiError && error.status === 409) {
      setBinding(null); setMessage('The run or evidence changed. Review the refreshed state before acting.');
      await onRefresh().catch(() => {});
    } else setMessage('The request could not be confirmed. Retry after checking the connection.');
  }

  async function prepare(name: string) {
    if (busy.current || disabled || !lifecycle.current) return;
    const signal = lifecycle.current.signal;
    busy.current = true; setPending(true); setMessage(''); setFeedback(''); setBinding(null);
    try {
      const fresh = await onRefresh();
      signal.throwIfAborted();
      const command = fresh.available_commands.find(item => item.name === name);
      if (fresh.run.id !== projection.run.id || !command || command.expected_run_version !== fresh.run.version) throw new ApiError(409, 'stale-projection');
      const next: Binding = { command, key: crypto.randomUUID() };
      if (name === 'approve_plan' || name === 'approve_pr' || name === 'approve_merge') {
        if (command.gate !== name.slice('approve_'.length) || !command.evidence_digest || !command.policy_version) throw new ApiError(409, 'stale-projection');
        const artifact = await api<components['schemas']['ArtifactTextResponse']>(`/artifacts/${command.evidence_digest}/text`, { signal });
        if (!artifact || artifact.digest !== command.evidence_digest) throw new Error('Evidence unavailable');
        const evidence: unknown = JSON.parse(artifact.text);
        if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) throw new Error('Evidence unavailable');
        next.evidence = evidence as Record<string, unknown>;
        if (name === 'approve_pr') {
          next.pr = parsePrEvidence(next.evidence);
          if (next.pr.candidate_commit !== fresh.candidate.commit || next.pr.repository !== fresh.project.github_repository
            || next.pr.base_sha !== fresh.run.base_sha || next.pr.base_ref !== fresh.run.base_ref
            || next.pr.validation_digest !== fresh.candidate.validation_evidence_digest
            || next.pr.review_digest !== fresh.candidate.review_evidence_digest
            || next.pr.runner_mode !== fresh.security.runner_mode) throw new ApiError(409, 'publication-evidence-mismatch');
          const body = await api<components['schemas']['ArtifactTextResponse']>(`/artifacts/${next.pr.body_digest}/text`, { signal });
          if (body?.digest !== next.pr.body_digest || typeof body.text !== 'string') throw new ApiError(409, 'body-evidence-mismatch');
          next.body = body.text;
        }
        if (name === 'approve_merge') {
          next.merge = parseMergeEvidence(next.evidence);
          const merge = next.merge, pr = fresh.pull_request, observation = fresh.remote_observation;
          if (!pr || !observation || merge.repository !== pr.repository || merge.pull_request_number !== pr.number
            || merge.head_sha !== pr.head_sha || merge.head_sha !== fresh.candidate.commit || merge.head_sha !== observation.head_sha
            || merge.base_ref !== pr.base_ref || merge.policy_version !== command.policy_version
            || merge.validation_digest !== fresh.candidate.validation_evidence_digest || merge.review_digest !== fresh.candidate.review_evidence_digest
            || merge.runner_mode !== fresh.security.runner_mode) throw new ApiError(409, 'merge-evidence-mismatch');
          const proof = await api<components['schemas']['MergeProtectionResponse']>(`/artifacts/${observation.observation_digest}/merge-protection`, { signal });
          if (!proof || proof.digest !== observation.observation_digest || proof.protection_digest !== merge.protection_digest
            || proof.repository !== merge.repository || proof.pull_request_number !== merge.pull_request_number
            || proof.head_sha !== merge.head_sha || proof.base_ref !== merge.base_ref || proof.observed_base_sha !== merge.base_sha
            || !proof.protection.verified || proof.protection.actor_can_bypass
            || !(proof.protection.strict_required_checks || proof.protection.merge_queue_enabled)
            || !proof.protection.required_check_names.length
            || proof.protection.required_check_names.some(name => !Object.hasOwn(merge.required_checks, name) && !Object.hasOwn(merge.required_checks, `status:${name}`))) throw new ApiError(409, 'merge-protection-mismatch');
          next.protection = proof.protection;
        }
        next.challenge = await api<components['schemas']['ApprovalChallengeResponse']>(`/runs/${fresh.run.id}/approval-challenges`, {
          method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ gate: command.gate, run_version: command.expected_run_version,
            evidence_digest: command.evidence_digest, policy_version: command.policy_version }),
        });
        if (!next.challenge?.token) throw new Error('Challenge unavailable');
      }
      if (name === 'teardown_run_resources') {
        const prefix = `teardown:${fresh.run.id}:`;
        if (!fresh.resource.teardown_confirmation.startsWith(prefix) || !/^[a-f0-9]{64}$/.test(fresh.resource.teardown_confirmation.slice(prefix.length))) throw new ApiError(409, 'invalid-resource-confirmation');
        next.resource = { ...fresh.resource };
      }
      if (!signal.aborted) setBinding(next);
    } catch (error) { await report(error, signal); }
    finally { busy.current = false; if (!signal.aborted) setPending(false); }
  }

  async function confirm(deleteBranch = false) {
    if (!binding || busy.current || stale || disabled || !lifecycle.current) return;
    if (binding.command.requires_feedback && !feedback.trim() && !binding.payload) return;
    const signal = lifecycle.current.signal;
    busy.current = true; setPending(true); setMessage('');
    try {
      if (binding.challenge) {
        const result = await api<components['schemas']['ApprovalResponse']>(`/runs/${projection.run.id}/approvals`, {
          method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ gate: binding.command.gate, run_version: binding.command.expected_run_version,
            evidence_digest: binding.command.evidence_digest, challenge_token: binding.challenge.token }),
        });
        if (!result?.approval_id) throw new Error('Approval response unavailable');
      } else {
        const payload = binding.payload ?? (binding.resource ? {
          command_type: binding.command.name, delete_branch: deleteBranch,
          confirm_resource_identity: binding.resource.teardown_confirmation,
          ...(deleteBranch ? { confirm_branch_name: binding.resource.branch_name } : {}),
        } : { command_type: binding.command.name,
          ...(binding.command.requires_feedback ? { feedback } : {}) });
        setBinding({ ...binding, payload }); // Unknown responses retry the identical command body/key.
        const result = await mutate<components['schemas']['RunCommandResponse']>(`/runs/${projection.run.id}/commands`, payload,
          { idempotencyKey: binding.key, expectedVersion: binding.command.expected_run_version, signal });
        if (!result?.id) throw new Error('Command response unavailable');
      }
      if (!signal.aborted) { setBinding(null); await onRefresh(); }
    } catch (error) { await report(error, signal); }
    finally { busy.current = false; if (!signal.aborted) setPending(false); }
  }

  return <section aria-label="Run controls">
    {projection.available_commands.filter(command => labels[command.name]).map(command => <button key={command.name}
      disabled={disabled || pending} onClick={() => void prepare(command.name)}>{labels[command.name]}</button>)}
    {pending && <p role="status">Checking current run evidence…</p>}
    {message && <p role="alert">{message}</p>}
    {stale && <p role="alert">The run changed. Open the action again to review current evidence.</p>}
    {binding && !stale && <ConfirmationDialog title={labels[binding.command.name]} onClose={() => setBinding(null)}>
      <p>Run version {binding.command.expected_run_version}</p>
      {binding.command.name === 'cancel' && <p>The run will stop; resources and evidence remain available until explicit teardown.</p>}
      {binding.command.name === 'pause' && <p>Pause further work after the current durable activity settles.</p>}
      {binding.command.name === 'resume' && <p>Resume within the run’s retained policy and remaining budgets.</p>}
      {binding.evidence && <><p>Evidence digest: <code>{binding.command.evidence_digest}</code></p>
        <p>Policy version {binding.command.policy_version}</p>
        {binding.merge && binding.protection ? <MergeApprovalEvidence evidence={binding.merge} protection={binding.protection} /> : binding.pr ? <PrPublicationEvidence evidence={binding.pr} body={binding.body ?? ''} /> : <pre className="policy-document">{JSON.stringify(binding.evidence, null, 2)}</pre>}
        <p>Challenge expires {binding.challenge?.expires_at}</p></>}
      {binding.command.requires_feedback && <label>Revision feedback<textarea required maxLength={8000} rows={5}
        value={feedback} readOnly={!!binding.payload} onChange={event => setFeedback(event.target.value)} /></label>}
      {binding.resource ? <ResourceTeardown key={binding.resource.teardown_confirmation} resource={binding.resource}
        disabled={pending || disabled} onConfirm={deleteBranch => void confirm(deleteBranch)} /> : <button disabled={pending || disabled || (binding.command.requires_feedback && !feedback.trim())}
        onClick={() => void confirm()}>Confirm {labels[binding.command.name].toLowerCase()}</button>}
    </ConfirmationDialog>}
  </section>;
}

function ConfirmationDialog({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement;
    const element = dialog.current;
    element?.showModal();
    return () => { element?.close(); if (previous instanceof HTMLElement) previous.focus(); };
  }, []);
  return <dialog ref={dialog} className="confirmation" aria-label={title} onClose={onClose}>
    <h2>{title}</h2>{children}<button onClick={onClose}>Back</button>
  </dialog>;
}
