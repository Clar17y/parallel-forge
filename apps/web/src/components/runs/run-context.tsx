import { useRef, useState, type FormEvent } from 'react';
import type { components } from '@/lib/api/schema';
import { ApiError, mutate } from '@/lib/api/client';
import { Panel } from '@/components/ui/panel';
import { Button } from '@/components/ui/button';
import { AgentStatus } from './agent-status';
import { AutonomyBanner } from './autonomy-banner';
import { nextGateMessage, readableLabel } from './run-presentation';

export function RunContext({
  projection: p,
  onTasks,
  onChanged,
}: {
  projection: components['schemas']['RunProjection'];
  onTasks?: () => void;
  onChanged?: () => void;
}) {
  const profile = p.run.subscription_profile;
  const profileSource = profile?.selection_source === 'project_default'
    ? 'Project default'
    : profile?.selection_source === 'run_override' ? 'Chosen for this run' : 'Source unknown';

  const [isEditing, setIsEditing] = useState(false);
  const [reviewPlanFirst, setReviewPlanFirst] = useState(p.run.workflow?.review_plan_first ?? false);
  const [delegation, setDelegation] = useState<'auto' | 'disabled' | 'requested'>(p.run.workflow?.delegation ?? 'auto');
  const [optionalReview, setOptionalReview] = useState<'auto' | 'skip' | 'required'>(p.run.workflow?.optional_review ?? 'auto');
  const [note, setNote] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [canRetry, setCanRetry] = useState(false);

  const attempt = useRef<{
    key: string;
    payload: components['schemas']['RunWorkflowUpdateRequest'];
  } | null>(null);
  const busy = useRef(false);

  const isTerminal = ['COMPLETED', 'FAILED', 'CANCELLED'].includes(p.run.state);

  function startEditing() {
    if (p.run.workflow) {
      setReviewPlanFirst(p.run.workflow.review_plan_first ?? false);
      setDelegation(p.run.workflow.delegation ?? 'auto');
      setOptionalReview(p.run.workflow.optional_review ?? 'auto');
      setNote('');
      setError('');
      setCanRetry(false);
      attempt.current = null;
      setIsEditing(true);
    }
  }

  function cancelEditing() {
    setIsEditing(false);
    setError('');
    setCanRetry(false);
    attempt.current = null;
  }

  async function submitWorkflow(event: FormEvent) {
    event.preventDefault();
    if (busy.current || !p.run.workflow) return;
    busy.current = true;
    setPending(true);
    setError('');

    attempt.current ??= {
      key: crypto.randomUUID(),
      payload: {
        expected_run_version: p.run.version,
        expected_workflow: p.run.workflow,
        workflow: {
          review_plan_first: reviewPlanFirst,
          delegation,
          optional_review: optionalReview,
        },
        ...(note.trim() ? { note: note.trim() } : {}),
      },
    };

    const current = attempt.current;
    try {
      await mutate<components['schemas']['RunResponse']>(
        `/runs/${p.run.id}/workflow`,
        current.payload,
        {
          method: 'PUT',
          idempotencyKey: current.key,
        }
      );
      attempt.current = null;
      setCanRetry(false);
      setIsEditing(false);
      onChanged?.();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError('Run version or workflow preferences have changed concurrently. Refreshed latest state; your draft has been retained.');
        attempt.current = null;
        setCanRetry(false);
        onChanged?.();
      } else if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
        setError('Workflow preferences were rejected. Adjust them and save again.');
        attempt.current = null;
        setCanRetry(false);
      } else {
        setError('Failed to update workflow preferences. Retry the same request to confirm its outcome.');
        setCanRetry(true);
      }
    } finally {
      busy.current = false;
      setPending(false);
    }
  }

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
        <div><dt>Subscription profile</dt><dd>{profile
          ? <><code>{profile.profile_id}</code> · version {profile.profile_version} · {profileSource}</>
          : 'No subscription profile frozen for this run'}</dd></div>
        <div>
          <dt>Workflow</dt>
          <dd>
            {p.run.workflow
              ? `${p.run.workflow.review_plan_first ? 'Review plan first' : 'Ordinary (prepare plan internally)'} · Delegation: ${p.run.workflow.delegation} · Review: ${p.run.workflow.optional_review}`
              : 'Legacy workflow (plan approval required)'}
            {p.run.workflow && !isTerminal && !isEditing && (
              <div style={{ marginTop: '6px' }}>
                <Button variant="quiet" onClick={startEditing}>
                  Edit preferences
                </Button>
              </div>
            )}
          </dd>
        </div>
        {isEditing && (
          <div style={{ gridColumn: '1 / -1' }}>
            <form onSubmit={submitWorkflow} aria-label="Edit workflow preferences" style={{ marginTop: '8px', marginBottom: '8px', padding: '10px', border: '1px solid var(--border)', borderRadius: '6px' }}>
              <p className="meta">Preferences apply to future decisions. Completed planning and reviews keep their existing results.</p>
              <div className="form-field" style={{ marginBottom: '8px' }}>
                <label className="checkbox-label" htmlFor="edit-review-plan-first" style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    id="edit-review-plan-first"
                    checked={reviewPlanFirst}
                    disabled={pending || canRetry}
                    onChange={e => setReviewPlanFirst(e.target.checked)}
                  />
                  <span style={{ fontWeight: 500 }}>Review plan before implementation</span>
                </label>
              </div>
              <div className="form-field" style={{ marginBottom: '8px' }}>
                <label htmlFor="edit-delegation">Delegation</label>
                <select
                  id="edit-delegation"
                  value={delegation}
                  disabled={pending || canRetry}
                  onChange={e => setDelegation(e.target.value as 'auto' | 'disabled' | 'requested')}
                >
                  <option value="auto">Auto</option>
                  <option value="disabled">Disabled</option>
                  <option value="requested">Requested</option>
                </select>
              </div>
              <div className="form-field" style={{ marginBottom: '8px' }}>
                <label htmlFor="edit-optional-review">Optional review</label>
                <select
                  id="edit-optional-review"
                  value={optionalReview}
                  disabled={pending || canRetry}
                  onChange={e => setOptionalReview(e.target.value as 'auto' | 'skip' | 'required')}
                >
                  <option value="auto">Auto</option>
                  <option value="skip">Skip</option>
                  <option value="required">Required</option>
                </select>
              </div>
              <div className="form-field" style={{ marginBottom: '8px' }}>
                <label htmlFor="edit-workflow-note">Note (optional)</label>
                <input
                  id="edit-workflow-note"
                  type="text"
                  value={note}
                  disabled={pending || canRetry}
                  maxLength={4096}
                  onChange={e => setNote(e.target.value)}
                  placeholder="Optional note for this update"
                />
              </div>
              {error && <p role="alert" className="field-error" style={{ marginBottom: '8px' }}>{error}</p>}
              <div style={{ display: 'flex', gap: '8px' }}>
                <Button type="submit" variant="primary" disabled={pending}>
                  {pending ? 'Saving…' : (error && canRetry) ? 'Retry update' : 'Save preferences'}
                </Button>
                <Button type="button" variant="quiet" onClick={cancelEditing} disabled={pending}>
                  Cancel
                </Button>
              </div>
            </form>
          </div>
        )}
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
