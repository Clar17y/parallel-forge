'use client';

import { useEffect, useState, type FormEvent } from 'react';
import Link from 'next/link';
import { Button } from '@/components/ui/button';
import { Panel } from '@/components/ui/panel';
import { StatusBadge } from '@/components/ui/status-badge';
import { useEpicExecution } from '@/hooks/epics/use-epic-execution';
import { useEpicWorkspace } from '@/hooks/epics/use-epic-workspace';
import { EvidenceDetails } from './evidence-details';
import { EpicBudgetPanel } from './epic-budget-panel';

export function DeliveryWorkspace({
  epicId,
  initialExecutionId,
  epicVersion,
}: {
  epicId: string;
  initialExecutionId?: string | null;
  epicVersion: number;
}) {
  const delivery = useEpicExecution(epicId, initialExecutionId);
  const workspace = useEpicWorkspace(epicId);
  const [manualIdInput, setManualIdInput] = useState('');
  const [actionNotice, setActionNotice] = useState<string | null>(null);

  // Owner override start controls
  const [isStartingNew, setIsStartingNew] = useState(false);
  const [useOwnerOverride, setUseOwnerOverride] = useState(false);
  const [selectedBriefId, setSelectedBriefId] = useState('');
  const [selectedGraphId, setSelectedGraphId] = useState('');
  const [overrideNote, setOverrideNote] = useState('');

  const {
    executionId,
    setExecutionId,
    executions,
    executionsLoading,
    executionsFailed,
    execution,
    loading,
    failed,
    refresh,
    isPendingDiscovery,
    controlState,
    controlVersion,
    children,
    intents,
    ownerActions,
    blockerCode,
    startExecution,
    sendCommand,
    mutations,
  } = delivery;
  const registerCompletion = mutations.registerCompletion;

  useEffect(() => {
    const unregisterStart = registerCompletion('execution-start', value => {
      if ((value as { execution_id?: string }).execution_id) {
        setActionNotice('Execution started; loading frozen progress.');
      }
    });
    const unregisterCommand = registerCompletion('execution-command', (_value, request) => {
      const action = request.body.action;
      if (action === 'pause' || action === 'resume' || action === 'cancel') {
        const label = (action as string)[0].toUpperCase() + (action as string).slice(1);
        setActionNotice(`${label} requested; waiting for server confirmation.`);
      }
    });
    return () => { unregisterStart(); unregisterCommand(); };
  }, [registerCompletion]);

  const handleStart = async () => {
    try {
      if (useOwnerOverride) {
        const chosenBrief = workspace.briefRevisions.find(b => b.brief_revision_id === selectedBriefId);
        const chosenGraph = workspace.graphRevisions.find(g => g.graph_revision_id === selectedGraphId);
        await startExecution({
          expectedEpicVersion: epicVersion,
          briefRevisionId: chosenBrief?.brief_revision_id ?? workspace.acceptedBrief?.brief_revision_id,
          briefDigest: chosenBrief?.content_digest ?? workspace.acceptedBrief?.brief_digest,
          graphRevisionId: chosenGraph?.graph_revision_id ?? workspace.acceptedGraph?.graph_revision_id,
          graphDigest: chosenGraph?.graph_digest ?? workspace.acceptedGraph?.graph_digest,
          ownerOverride: true,
          overrideNote: overrideNote.trim() || undefined,
        });
      } else {
        await startExecution({
          expectedEpicVersion: epicVersion,
          briefRevisionId: workspace.acceptedBrief?.brief_revision_id,
          briefDigest: workspace.acceptedBrief?.brief_digest,
          graphRevisionId: workspace.acceptedGraph?.graph_revision_id,
          graphDigest: workspace.acceptedGraph?.graph_digest,
          ownerOverride: false,
        });
      }
      setIsStartingNew(false);
    } catch {
      // Mutations hook captures errors
    }
  };

  const handleCommand = async (action: 'pause' | 'resume' | 'cancel') => {
    if (!execution || controlVersion === null) return;
    try {
      await sendCommand(action, controlVersion);
    } catch {
      // Mutations hook captures errors
    }
  };

  const handleManualIdSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!mutations.hasPendingRetry && !mutations.loading && manualIdInput.trim()) {
      setActionNotice(null);
      setExecutionId(manualIdInput.trim());
    }
  };

  const executionSnapshot = execution?.execution ?? null;
  const briefRevId = executionSnapshot?.brief_revision_id ?? null;
  const graphRevId = executionSnapshot?.graph_revision_id ?? null;
  const briefDigest = executionSnapshot?.brief_digest ?? null;
  const graphDigest = executionSnapshot?.graph_digest ?? null;

  const frozenGraph = graphRevId
    ? workspace.graphRevisions.find(revision => revision.graph_revision_id === graphRevId)
    : undefined;
  const frozenItems = new Map((frozenGraph?.items ?? []).map(item => [item.item_id, item]));
  const mutationPending = mutations.loading || mutations.hasPendingRetry;
  const isDeliveryAction = !mutations.actionKind || ['execution-start', 'execution-command'].includes(mutations.actionKind);

  const effectiveState = (controlState ?? '').toUpperCase();
  const isSucceeded = effectiveState === 'SUCCEEDED';
  const isPaused = effectiveState === 'PAUSED';
  const isActive = effectiveState === 'ACTIVE';
  const isBlocked = effectiveState === 'BLOCKED';
  const isCancelled = effectiveState === 'CANCELLED';
  const isRequested = effectiveState.includes('REQUESTED');

  const hasUnsettledChildren = children.some(c => !c.effects_settled);
  const hasControlVersion = controlVersion !== null;

  return (
    <div className="delivery-workspace space-y-6">
      {/* Uncertainty Retry Banner */}
      {mutations.hasPendingRetry && !mutations.shared && (
        <div role="alert" className="p-4 bg-[var(--warning-soft)] text-[var(--warning)] rounded border border-[var(--border)] space-y-2">
          <p className="font-semibold">Network or server error. Mutation outcome uncertain.</p>
          <div className="flex space-x-2">
            <Button variant="primary" disabled={mutations.loading} onClick={() => { void mutations.retryPending().catch(() => undefined); }}>
              {mutations.loading ? 'Retrying…' : 'Retry original request'}
            </Button>
          </div>
        </div>
      )}

      {mutations.conflict && isDeliveryAction && (
        <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)] space-y-1">
          <p className="font-semibold">
            {mutations.actionKind === 'execution-command'
              ? 'Conflict: Execution version has changed concurrently.'
              : 'Conflict: The epic version changed on the server before starting execution.'}
          </p>
          <Button variant="secondary" onClick={() => { mutations.clearError(); refresh(); }}>
            Refresh execution
          </Button>
        </div>
      )}

      {mutations.error && !mutations.conflict && isDeliveryAction && (
        <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)] space-y-2">
          <p>{mutations.error}</p>
          <Button variant="quiet" onClick={mutations.clearError}>Dismiss message</Button>
        </div>
      )}

      {actionNotice && (
        <div role="status" className="p-3 bg-[var(--success-soft)] text-[var(--success)] rounded text-sm">
          {actionNotice}
        </div>
      )}

      {/* Discovered Executions Bar */}
      {executions.length > 0 && (
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 p-3 bg-[var(--surface-muted)] border border-[var(--border)] rounded text-sm">
          <div className="flex items-center gap-2">
            <label htmlFor="select-execution" className="font-medium text-xs text-[var(--muted)] uppercase">
              Discovered Executions:
            </label>
            <select
              id="select-execution"
              aria-label="Select Execution"
              className="px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
              value={executionId ?? ''}
              disabled={mutationPending}
              onChange={e => {
                setActionNotice(null);
                setExecutionId(e.target.value || null);
              }}
            >
              <option value="">Select an execution...</option>
              {executions.map(ex => (
                <option key={ex.execution.execution_id} value={ex.execution.execution_id}>
                  {ex.execution.execution_id.slice(0, 8)}... ({ex.control_state ?? 'legacy'}) • {ex.execution.created_at}
                </option>
              ))}
            </select>
          </div>
          {!isStartingNew && (
            <Button
              variant="secondary"
              disabled={mutationPending}
              onClick={() => setIsStartingNew(true)}
            >
              Start New Execution
            </Button>
          )}
        </div>
      )}

      {/* Discovery / Start Form (shown when pending discovery OR explicitly requested) */}
      {(isPendingDiscovery || isStartingNew) && (
        <Panel
          title={isPendingDiscovery ? 'Execution Discovery & Start' : 'Start New Execution Epoch'}
          description="Start an execution from current accepted sources or custom revisions, or load by ID."
        >
          <div className="space-y-4">
            <div className="p-4 bg-[var(--surface-muted)] border border-[var(--border)] rounded text-sm text-[var(--muted)] space-y-2">
              <p>
                {executionsLoading
                  ? 'Discovering executions from server…'
                  : executionsFailed
                  ? 'Failed to discover executions. You can retry discovery, enter an execution ID, or start a new execution.'
                  : executions.length === 0
                  ? 'No executions discovered for this epic yet. You can start an execution using the current accepted pair or alternate saved sources, or load an execution by ID.'
                  : 'Start a new execution epoch. Previous frozen executions will remain available in history.'}
              </p>
            </div>

            <form onSubmit={handleManualIdSubmit} className="flex flex-col sm:flex-row gap-2">
              <label className="sr-only" htmlFor="execution-id">Execution ID</label>
              <input
                id="execution-id"
                type="text"
                className="flex-1 px-3 py-2 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
                placeholder="Enter execution ID (UUID)..."
                value={manualIdInput}
                disabled={mutationPending}
                onChange={e => setManualIdInput(e.target.value)}
              />
              <Button type="submit" variant="secondary" disabled={mutationPending || !manualIdInput.trim()}>
                Load Execution
              </Button>
              <Button type="button" variant="primary" disabled={mutationPending} onClick={handleStart}>
                Start Execution
              </Button>
              {isStartingNew && !isPendingDiscovery && (
                <Button type="button" variant="quiet" onClick={() => setIsStartingNew(false)}>
                  Cancel
                </Button>
              )}
            </form>

            {/* Direct Owner Source Selection / Override */}
            <div className="pt-2 border-t border-[var(--border)] space-y-3 text-sm">
              <label className="flex items-center gap-2 cursor-pointer text-xs font-medium text-[var(--muted)]">
                <input
                  type="checkbox"
                  checked={useOwnerOverride}
                  disabled={mutationPending}
                  onChange={e => setUseOwnerOverride(e.target.checked)}
                />
                <span>Owner Override: Select custom saved brief / graph sources</span>
              </label>

              {useOwnerOverride && (
                <div className="p-3 border border-[var(--warning)] bg-[var(--warning-soft)] rounded space-y-3">
                  <p className="text-xs font-semibold text-[var(--warning)]">
                    Warning: Starting execution with non-default or non-accepted sources.
                  </p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                      <label htmlFor="override-brief" className="block text-xs font-medium">Brief Revision</label>
                      <select
                        id="override-brief"
                        className="w-full mt-1 px-2 py-1 border border-[var(--control-border)] rounded text-xs bg-[var(--surface)]"
                        value={selectedBriefId}
                        onChange={e => setSelectedBriefId(e.target.value)}
                        disabled={mutationPending}
                      >
                        <option value="">Accepted brief (default)</option>
                        {workspace.briefRevisions.map(b => (
                          <option key={b.brief_revision_id} value={b.brief_revision_id}>
                            Rev #{b.revision_number} ({b.brief_revision_id.slice(0, 8)}...)
                          </option>
                        ))}
                      </select>
                    </div>

                    <div>
                      <label htmlFor="override-graph" className="block text-xs font-medium">Graph Revision</label>
                      <select
                        id="override-graph"
                        className="w-full mt-1 px-2 py-1 border border-[var(--control-border)] rounded text-xs bg-[var(--surface)]"
                        value={selectedGraphId}
                        onChange={e => setSelectedGraphId(e.target.value)}
                        disabled={mutationPending}
                      >
                        <option value="">Accepted graph (default)</option>
                        {workspace.graphRevisions.map(g => (
                          <option key={g.graph_revision_id} value={g.graph_revision_id}>
                            Rev #{g.revision_number} ({g.graph_revision_id.slice(0, 8)}...)
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>

                  <div>
                    <label htmlFor="override-note" className="block text-xs font-medium">Override Note (optional)</label>
                    <input
                      id="override-note"
                      type="text"
                      className="w-full mt-1 px-2 py-1 border border-[var(--control-border)] rounded text-xs bg-[var(--surface)]"
                      placeholder="Reason for starting with non-accepted sources..."
                      value={overrideNote}
                      onChange={e => setOverrideNote(e.target.value)}
                      disabled={mutationPending}
                    />
                  </div>
                </div>
              )}
            </div>
          </div>
        </Panel>
      )}

      {loading && !execution && <p role="status">Loading execution projection…</p>}

      {failed && !execution && (
        <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)] space-y-2">
          <p>Failed to load execution {executionId}.</p>
          <Button variant="secondary" onClick={refresh}>
            Retry
          </Button>
        </div>
      )}

      {failed && execution && (
        <div role="status" className="p-3 bg-[var(--warning-soft)] text-[var(--warning)] rounded border border-[var(--border)] text-sm">
          Progress could not be refreshed. Showing the last server projection.
          <Button variant="quiet" className="ml-2" onClick={refresh}>Retry refresh</Button>
        </div>
      )}

      {execution && (
        <div className="space-y-6">
          {/* Header Panel */}
          <Panel
            title="Frozen Execution Progress"
            description={`${hasControlVersion ? `Execution Version ${controlVersion}` : 'Legacy execution (control version unavailable)'} • Bound to Epic Version ${epicVersion}`}
            className="[&_.panel-heading]:flex-col [&_.panel-heading]:items-start"
            action={undefined}
          >
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge
                  label={effectiveState.replaceAll('_', ' ') || 'Unknown'}
                  tone={
                    isSucceeded
                      ? 'success'
                      : isBlocked || isCancelled
                      ? 'danger'
                      : isPaused || isRequested
                      ? 'warning'
                      : 'info'
                  }
                />
                {!hasControlVersion && (
                  <StatusBadge label="Legacy (control unavailable)" tone="neutral" />
                )}
              </div>

              <EvidenceDetails summary="Inspect Execution ID">
                <span>execution_id: {executionId}</span>
              </EvidenceDetails>
              <EvidenceDetails summary="Inspect frozen brief and graph revisions">
                <span>brief_revision_id: {briefRevId}</span>
                {briefDigest && <span>brief_digest: {briefDigest}</span>}
                <span>graph_revision_id: {graphRevId}</span>
                {graphDigest && <span>graph_digest: {graphDigest}</span>}
              </EvidenceDetails>

              {/* Truthful Success Display (only when server state is SUCCEEDED) */}
              {isSucceeded && (
                <div role="status" className="p-4 bg-[var(--success-soft)] border border-[var(--success)] rounded text-sm space-y-2">
                  <p className="font-semibold text-[var(--success)]">
                    Execution completed with status: SUCCEEDED.
                  </p>
                </div>
              )}

              {/* Unsettled cancellation warning */}
              {(isCancelled || effectiveState === 'CANCEL_REQUESTED') && hasUnsettledChildren && (
                <div role="status" className="p-3 bg-[var(--warning-soft)] border border-[var(--warning)] rounded text-sm text-[var(--warning)]">
                  Unsettled child runs remain. Effects not yet settled.
                </div>
              )}

              {/* Execution Controls: Pause, Resume, Cancel */}
              <div className="pt-2 flex flex-wrap gap-2 border-t border-[var(--border)]">
                <Button
                  variant="secondary"
                  disabled={mutationPending || !hasControlVersion || isRequested || (!isActive && !isBlocked)}
                  onClick={() => handleCommand('pause')}
                >
                  Pause Execution
                </Button>
                <Button
                  variant="secondary"
                  disabled={mutationPending || !hasControlVersion || isRequested || (!isPaused && !isBlocked)}
                  onClick={() => handleCommand('resume')}
                >
                  Resume Execution
                </Button>
                <Button
                  variant="danger"
                  disabled={mutationPending || !hasControlVersion || isRequested || (!isActive && !isPaused && !isBlocked)}
                  onClick={() => handleCommand('cancel')}
                >
                  Cancel Execution
                </Button>
                <Button variant="quiet" onClick={refresh}>
                  Refresh Progress
                </Button>
              </div>
            </div>
          </Panel>

          {/* Next Actions & Blockers */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Panel title="Next Actions">
              <p className="text-sm">
                {isSucceeded
                  ? 'Execution completed (status SUCCEEDED).'
                  : isBlocked
                  ? 'Resolve the reported blockers before continuing, or perform owner recovery.'
                  : isCancelled
                  ? 'The execution is cancelled.'
                  : isRequested
                  ? 'The requested change is still being processed.'
                  : isPaused
                  ? 'The execution is paused. Resume it when ready; any active child gate remains in place.'
                  : children.some(c => c.pending_gate || c.retained_gate)
                  ? `Review the ${children.find(c => c.pending_gate || c.retained_gate)?.pending_gate ?? children.find(c => c.pending_gate || c.retained_gate)?.retained_gate} gate for the active child run.`
                  : 'Execution is active.'}
              </p>
            </Panel>

            <Panel title="Active Blockers">
              {!blockerCode && !children.some(c => c.attempt.blocker_codes?.length) ? (
                <p className="text-sm text-[var(--muted)]">No active blockers.</p>
              ) : (
                <ul className="list-disc pl-5 text-sm text-[var(--danger)] space-y-1">
                  {blockerCode && <li>{blockerCode}</li>}
                  {children.flatMap(c => c.attempt.blocker_codes ?? []).map((code, idx) => (
                    <li key={`child-${idx}`}>{code}</li>
                  ))}
                </ul>
              )}
            </Panel>
          </div>

          {/* Recent Intents & Refusals */}
          {intents.length > 0 ? (
            <Panel title="Recent Control Intents & Refusals" description="Tracks requested and processed execution controls.">
              <div className="space-y-2 text-sm">
                {intents.map(intent => (
                  <div key={intent.intent_id} className="p-3 border border-[var(--border)] rounded flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold uppercase text-xs">{intent.action}</span>
                      <StatusBadge label={intent.status} tone={intent.status === 'refused' ? 'danger' : intent.status === 'executed' ? 'success' : 'warning'} />
                      <span className="text-xs text-[var(--muted)]">v{intent.control_version}</span>
                    </div>
                    {intent.refusal && (
                      <p className="text-xs text-[var(--danger)] font-medium">Refusal: {intent.refusal}</p>
                    )}
                  </div>
                ))}
              </div>
            </Panel>
          ) : null}

          {/* Owner Actions & Audit Warnings */}
          {ownerActions.length > 0 && (
            <Panel title="Owner Actions & Audit Warnings" description="Authoritative history of owner interventions and warnings.">
              <div className="space-y-2 text-sm">
                {ownerActions.map((oa, idx) => (
                  <div key={idx} className="p-3 border border-[var(--border)] rounded space-y-1 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold">{oa.event_type}</span>
                      <span className="text-xs text-[var(--muted)]">Actor: {oa.actor_id}</span>
                    </div>
                    {oa.note && <p className="text-xs text-[var(--foreground)]">Note: {oa.note}</p>}
                    {oa.warnings.length > 0 && (
                      <ul className="list-disc pl-5 text-xs text-[var(--warning)]">
                        {oa.warnings.map((w, wIdx) => <li key={wIdx}>{w}</li>)}
                      </ul>
                    )}
                  </div>
                ))}
              </div>
            </Panel>
          )}

          {/* Child Runs & Approval Gates */}
          <Panel
            title="Linked Child Runs & Human Approval Gates"
            description="Child runs executed within Forge retain independent review and human approval boundaries."
          >
            {children.length === 0 ? (
              <p className="text-sm text-[var(--muted)]">No child run is currently active.</p>
            ) : (
              <div className="space-y-3">
                {children.map(run => {
                  const effectiveGate = run.pending_gate ?? run.retained_gate ?? null;
                  return (
                    <div
                      key={run.attempt.run_id}
                      className="p-3 border border-[var(--border)] rounded bg-[var(--surface)] flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-sm"
                    >
                      <div>
                        <div className="flex flex-wrap items-center gap-2 min-w-0">
                          <Link href={`/runs/${run.attempt.run_id}`} className="min-w-0 break-words font-medium text-[var(--focus)] hover:underline">
                            Run for {frozenItems.get(run.attempt.item_id)?.title ?? 'Child run'}
                          </Link>
                          <StatusBadge label={run.run_state.replaceAll('_', ' ')} tone="neutral" />
                          {effectiveGate && (
                            <StatusBadge
                              label={run.pending_gate ? run.pending_gate : `${run.retained_gate} (retained)`}
                              tone="warning"
                            />
                          )}
                          <StatusBadge
                            label={run.effects_settled ? 'Effects settled' : 'Effects unsettled'}
                            tone={run.effects_settled ? 'neutral' : 'warning'}
                          />
                        </div>
                        <p className="text-xs text-[var(--muted)] break-words">
                          {frozenItems.get(run.attempt.item_id)?.title ?? 'Work item'} · run version {run.run_version}
                        </p>
                        {run.attempt.blocker_codes.length > 0 && (
                          <p className="text-xs text-[var(--danger)]">
                            Blockers: {run.attempt.blocker_codes.join(', ')}
                          </p>
                        )}
                        {run.attempt.owner_override && (
                          <p className="text-xs text-[var(--warning)]">
                            Owner override: {run.attempt.override_note || 'Active'}
                          </p>
                        )}
                        <EvidenceDetails summary="Inspect run and work item IDs">
                          <span>run_id: {run.attempt.run_id}</span>
                          <span>item_id: {run.attempt.item_id}</span>
                          <span>attempt_id: {run.attempt.attempt_id}</span>
                          {run.retained_gate && <span>retained_gate: {run.retained_gate}</span>}
                        </EvidenceDetails>
                      </div>

                      {effectiveGate && (
                        <Link
                          href={`/runs/${run.attempt.run_id}`}
                          className="button text-xs self-start shrink-0 sm:self-auto"
                          data-variant="secondary"
                        >
                          Review Gate at {effectiveGate}
                        </Link>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </Panel>

          {/* Item Progression */}
          <Panel title="Work-Item Progression">
            {!frozenGraph && <p role="status" className="mb-3 text-sm text-[var(--muted)]">Details for this frozen graph are {workspace.loadingGraphRevisions ? 'loading' : 'unavailable'}.</p>}
            <div className="space-y-2">
              {(frozenGraph?.items ?? []).map((it, idx) => {
                const child = children.find(c => c.attempt.item_id === it.item_id);
                return (
                  <div key={it.item_id} className="p-3 border border-[var(--border)] rounded text-sm flex flex-col sm:flex-row sm:items-start sm:justify-between gap-2 min-w-0 break-words">
                    <div className="flex flex-wrap items-center gap-2 min-w-0">
                      <span className="font-semibold">#{idx + 1} {it.title}</span>
                      <StatusBadge label={it.disposition ?? 'required'} tone={it.disposition === 'deferred' ? 'warning' : 'neutral'} />
                      {child && <StatusBadge label={child.run_state.replaceAll('_', ' ')} tone="info" />}
                    </div>
                    {child && (
                      <Link href={`/runs/${child.attempt.run_id}`} className="text-xs text-[var(--focus)] hover:underline">
                        Open run
                      </Link>
                    )}
                    <EvidenceDetails summary="Inspect work item identity and digest">
                      <span>item_id: {it.item_id}</span>
                      {it.item_digest && <span>item_digest: {it.item_digest}</span>}
                    </EvidenceDetails>
                  </div>
                );
              })}
            </div>
          </Panel>

          {/* Shared Epic Budget Panel */}
          <EpicBudgetPanel epicId={epicId} />
        </div>
      )}
    </div>
  );
}
