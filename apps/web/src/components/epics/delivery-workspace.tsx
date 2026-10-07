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
import { useEpicWorkItemRuns } from '@/hooks/epics/use-epic-work-item-runs';
import type { EpicAttemptResponse } from '@/hooks/epics/types';

const blockerLabels: Record<string, string> = {
  epic_version_stale: 'The epic changed. Refresh before launching.',
  brief_not_accepted: 'The selected brief is not accepted for this execution.',
  graph_not_accepted: 'The selected graph is not accepted for this execution.',
  execution_not_active: 'This execution is not active.',
  item_deferred: 'This work item is saved as deferred.',
  active_child: 'Another child run is still active.',
  child_effects_unsettled: 'A previous child run still has unsettled effects.',
  repository_base_moved: 'The repository base changed since the execution snapshot.',
  predecessor_unverified: 'A predecessor has not been verified as integrated.',
  predecessor_integration_unverified: 'The predecessor is not verified as integrated.',
  integration_ref_unavailable: 'The project integration branch is unavailable.',
  integration_ref_changed: 'The integration branch changed during verification.',
  predecessor_not_started: 'The prerequisite work item has not started.',
  predecessor_failed: 'The prerequisite run failed or was cancelled.',
};

function blockerText(code: string | null | undefined): string {
  return code ? blockerLabels[code] ?? `The server reported: ${code.replaceAll('_', ' ')}.` : 'The server has not provided a blocker.';
}

function readinessLabel(status: string, blockerCode: string | null): string {
  if (status === 'verified') return 'Integrated and verified';
  if (status === 'ready') return 'Ready to launch';
  if (status === 'active') return 'Run active';
  if (status === 'deferred') return 'Deferred';
  return `Blocked: ${blockerText(blockerCode)}`;
}

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
  const workItemRuns = useEpicWorkItemRuns(epicId);
  const [manualIdInput, setManualIdInput] = useState('');
  const [actionNotice, setActionNotice] = useState<string | null>(null);

  // Owner override start controls
  const [isStartingNew, setIsStartingNew] = useState(false);
  const [useOwnerOverride, setUseOwnerOverride] = useState(false);
  const [selectedBriefId, setSelectedBriefId] = useState('');
  const [selectedGraphId, setSelectedGraphId] = useState('');
  const [overrideNote, setOverrideNote] = useState('');
  const [selectedItemId, setSelectedItemId] = useState('');
  const [launchOverride, setLaunchOverride] = useState(false);
  const [launchNote, setLaunchNote] = useState('');
  const [launchedAttempt, setLaunchedAttempt] = useState<EpicAttemptResponse | null>(null);

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
    setSequentialDispatch,
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
    const unregisterWorkItem = registerCompletion('work-item-launch', value => {
      setLaunchedAttempt(value as EpicAttemptResponse);
      setActionNotice('Work item launched; the run retains its normal approval gates.');
      setLaunchNote('');
      refresh();
    });
    const unregisterDispatch = registerCompletion('execution-dispatch', (_value, request) => {
      setActionNotice(request.body.enabled ? 'Sequential delivery enabled.' : 'Sequential delivery disabled.');
      refresh();
    });
    return () => { unregisterStart(); unregisterCommand(); unregisterWorkItem(); unregisterDispatch(); };
  }, [registerCompletion, refresh]);

  const savedGraphs = workspace.acceptedGraph && !workspace.graphRevisions.some(graph => graph.graph_revision_id === workspace.acceptedGraph?.graph_revision_id)
    ? [...workspace.graphRevisions, workspace.acceptedGraph]
    : workspace.graphRevisions;
  const graphsForBrief = (briefId: string) => {
    const brief = workspace.briefRevisions.find(revision => revision.brief_revision_id === briefId);
    return savedGraphs.filter(graph => graph.brief_revision_id === briefId && (!brief || graph.brief_digest === brief.content_digest));
  };
  const offeredGraphs = selectedBriefId ? graphsForBrief(selectedBriefId) : savedGraphs;
  const chosenGraph = selectedGraphId
    ? savedGraphs.find(graph => graph.graph_revision_id === selectedGraphId)
    : !selectedBriefId ? workspace.acceptedGraph : undefined;
  const sourceSelectionError = selectedBriefId && offeredGraphs.length === 0
    ? 'No matching graph revision available for this brief revision.'
    : !chosenGraph
      ? selectedGraphId
        ? 'Selected graph revision is unavailable. Choose a saved graph revision.'
        : 'Choose a saved graph revision for this execution.'
      : selectedBriefId && !offeredGraphs.some(graph => graph.graph_revision_id === chosenGraph.graph_revision_id)
        ? 'The selected graph does not match this brief revision. Choose a matching graph revision.'
        : null;

  const handleBriefChange = (briefId: string) => {
    setSelectedBriefId(briefId);
    if (!briefId) {
      setSelectedGraphId('');
      return;
    }
    const matching = graphsForBrief(briefId);
    if (!matching.some(graph => graph.graph_revision_id === selectedGraphId)) {
      setSelectedGraphId(matching.at(-1)?.graph_revision_id ?? '');
    }
  };

  const handleGraphChange = (graphId: string) => {
    setSelectedGraphId(graphId);
    const graph = savedGraphs.find(revision => revision.graph_revision_id === graphId);
    setSelectedBriefId(graph?.brief_revision_id ?? '');
  };

  const handleStart = async () => {
    try {
      if (useOwnerOverride) {
        if (sourceSelectionError || !chosenGraph) return;

        await startExecution({
          expectedEpicVersion: epicVersion,
          briefRevisionId: chosenGraph.brief_revision_id,
          briefDigest: chosenGraph.brief_digest,
          graphRevisionId: chosenGraph.graph_revision_id,
          graphDigest: chosenGraph.graph_digest,
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

  const handleDispatchToggle = async () => {
    if (!executionSnapshot || mutationPending) return;
    const current = execution?.dispatch;
    try {
      await setSequentialDispatch(
        !(current?.enabled ?? false),
        current?.version ?? 0,
        current?.profile_id ?? null,
        current?.profile_version ?? null,
      );
    } catch {
      // The mutation store retains version conflicts or the exact uncertain request.
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
      ?? (workspace.acceptedGraph?.graph_revision_id === graphRevId ? workspace.acceptedGraph : undefined)
    : undefined;
  const frozenItems = new Map((frozenGraph?.items ?? []).map(item => [item.item_id, item]));
  const mutationPending = mutations.loading || mutations.hasPendingRetry;
  const isDeliveryAction = !mutations.actionKind || ['execution-start', 'execution-command', 'execution-dispatch'].includes(mutations.actionKind);

  const effectiveState = (controlState ?? '').toUpperCase();
  const isSucceeded = effectiveState === 'SUCCEEDED';
  const isPaused = effectiveState === 'PAUSED';
  const isActive = effectiveState === 'ACTIVE';
  const isBlocked = effectiveState === 'BLOCKED';
  const isCancelled = effectiveState === 'CANCELLED';
  const isRequested = effectiveState.includes('REQUESTED');

  const hasUnsettledChildren = children.some(c => !c.effects_settled);
  const hasControlVersion = controlVersion !== null;
  const nextReadyItem = execution?.items?.find(item => item.status === 'ready');
  const firstBlockedItem = execution?.items?.find(item => item.status === 'blocked');

  const launchWorkItem = async () => {
    if (!executionSnapshot || !selectedItemId || mutationPending) return;
    try {
      await workItemRuns.launch({
        schema_version: 1,
        expected_epic_version: epicVersion,
        execution_id: executionSnapshot.execution_id,
        brief_revision_id: executionSnapshot.brief_revision_id,
        brief_digest: executionSnapshot.brief_digest,
        graph_revision_id: executionSnapshot.graph_revision_id,
        graph_digest: executionSnapshot.graph_digest,
        item_id: selectedItemId,
        owner_override: launchOverride,
        ...(launchOverride && launchNote.trim() ? { override_note: launchNote.trim() } : {}),
      });
    } catch {
      // The shared mutation store retains definitive blockers or the exact uncertain request.
    }
  };

  const launchBlockers = mutations.errorDetail?.blocker_codes ?? [];
  const isWorkItemLaunchError = mutations.actionKind === 'work-item-launch' && !!mutations.error;

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
              : mutations.actionKind === 'execution-dispatch'
              ? 'Conflict: Sequential delivery settings changed on the server.'
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
              <Button
                type="button"
                variant="primary"
                disabled={mutationPending || (useOwnerOverride && !!sourceSelectionError)}
                onClick={handleStart}
              >
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
                        onChange={e => handleBriefChange(e.target.value)}
                        disabled={mutationPending}
                      >
                        <option value="">Use graph-bound brief (default)</option>
                        {selectedBriefId && !workspace.briefRevisions.some(brief => brief.brief_revision_id === selectedBriefId) ? (
                          <option value={selectedBriefId}>Graph-bound brief ({selectedBriefId.slice(0, 8)}...)</option>
                        ) : null}
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
                        onChange={e => handleGraphChange(e.target.value)}
                        disabled={mutationPending || offeredGraphs.length === 0}
                      >
                        {!selectedBriefId ? (
                          <option value="">Accepted graph (default)</option>
                        ) : offeredGraphs.length === 0 ? (
                          <option value="">No matching graph available</option>
                        ) : null}
                        {offeredGraphs.map(g => (
                          <option key={g.graph_revision_id} value={g.graph_revision_id}>
                            {'revision_number' in g ? `Rev #${g.revision_number}` : 'Accepted graph'} ({g.graph_revision_id.slice(0, 8)}...)
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>

                  {sourceSelectionError ? (
                    <p role="alert" className="text-xs text-[var(--danger)]">
                      {sourceSelectionError}
                    </p>
                  ) : null}

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
            description={`${hasControlVersion ? `Execution Version ${controlVersion}` : 'Legacy execution (control version unavailable)'} • Current Epic Version ${epicVersion}`}
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
                  : nextReadyItem
                  ? `Next ready work item: ${frozenItems.get(nextReadyItem.item_id)?.title ?? 'Saved work item'}. Launch it below or enable sequential delivery.`
                  : firstBlockedItem
                  ? `Delivery is waiting: ${blockerText(firstBlockedItem.blocker_code)} Review its server evidence below.`
                  : execution?.items?.length
                  ? 'The server has no ready item to launch. Review the saved statuses below.'
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

          <Panel
            title="Sequential Delivery"
            description="When enabled, Forge may admit the next eligible saved item after integration is verified. Existing child plan, PR and merge gates remain in place."
          >
            <div className="space-y-3 text-sm">
              <p className="font-medium">Sequential delivery is {execution.dispatch?.enabled ? 'on.' : 'off.'}</p>
              {execution.dispatch?.blocker_code && (
                <div role="status" className="p-3 border border-[var(--warning)] bg-[var(--warning-soft)] rounded text-[var(--warning)]">
                  {blockerText(execution.dispatch.blocker_code)} The server will decide whether the requested change can proceed.
                </div>
              )}
              <p className="text-[var(--muted)]">
                The server currently reports {execution.dispatch?.enabled ? 'automatic sequential admission enabled' : 'automatic admission disabled'} for this execution. Enabling it does not approve a child run or declare integration complete.
              </p>
              <EvidenceDetails summary="Inspect sequential delivery settings">
                <span>dispatch version: {execution.dispatch?.version ?? 0}</span>
                <span>profile_id: {execution.dispatch?.profile_id ?? 'null'}</span>
                <span>profile_version: {execution.dispatch?.profile_version ?? 'null'}</span>
                <span>enabled_by_actor_id: {execution.dispatch?.enabled_by_actor_id ?? 'null'}</span>
                <span>claim_item_id: {execution.dispatch?.claim_item_id ?? 'null'}</span>
                <span>claim_expires_at: {execution.dispatch?.claim_expires_at ?? 'null'}</span>
                {execution.dispatch?.blocker_code && <span>blocker_code: {execution.dispatch.blocker_code}</span>}
              </EvidenceDetails>
              <Button
                variant={execution.dispatch?.enabled ? 'secondary' : 'primary'}
                disabled={mutationPending || !executionSnapshot || (mutations.conflict && mutations.actionKind === 'execution-dispatch')}
                onClick={() => { void handleDispatchToggle(); }}
              >
                {mutationPending && mutations.actionKind === 'execution-dispatch'
                  ? 'Saving delivery setting…'
                  : execution.dispatch?.enabled ? 'Disable Sequential Delivery' : 'Enable Sequential Delivery'}
              </Button>
            </div>
          </Panel>

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
            {(!execution.items || execution.items.length === 0) && <p className="mb-3 text-sm text-[var(--muted)]">No server readiness items are available for this execution; no status is inferred from the saved graph or child run state.</p>}
            <div className="space-y-2">
              {(frozenGraph?.items ?? []).map((it, idx) => {
                const child = children.find(c => c.attempt.item_id === it.item_id);
                const readiness = execution.items?.find(item => item.item_id === it.item_id);
                return (
                  <div key={it.item_id} className="p-3 border border-[var(--border)] rounded text-sm flex flex-col sm:flex-row sm:items-start sm:justify-between gap-2 min-w-0 break-words">
                    <div className="flex flex-wrap items-center gap-2 min-w-0">
                      <span className="font-semibold">#{idx + 1} {it.title}</span>
                      <StatusBadge label={it.disposition ?? 'required'} tone={it.disposition === 'deferred' ? 'warning' : 'neutral'} />
                      {readiness && <StatusBadge
                        label={readinessLabel(readiness.status, readiness.blocker_code)}
                        tone={readiness.status === 'verified' ? 'success' : readiness.status === 'blocked' ? 'danger' : readiness.status === 'deferred' ? 'warning' : 'info'}
                      />}
                      {!readiness && <StatusBadge label="Readiness unavailable" tone="warning" />}
                      {child && <span className="text-xs text-[var(--muted)]">Child run status: {child.run_state.replaceAll('_', ' ')}</span>}
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
                    {readiness && <EvidenceDetails summary="Inspect readiness evidence">
                      <span>server readiness: {readiness.status}</span>
                      <span>disposition: {readiness.disposition}</span>
                      <span>blocker_code: {readiness.blocker_code ?? 'null'}</span>
                      {readiness.completion_evidence && <div className="pt-1">
                        <span>completion evidence status: {readiness.completion_evidence.status}</span>
                        <span>completion blocker_code: {readiness.completion_evidence.blocker_code ?? 'null'}</span>
                        <span>predecessor_run_id: {readiness.completion_evidence.predecessor_run_id ?? 'null'}</span>
                        <span>integrated_sha: {readiness.completion_evidence.integrated_sha ?? 'null'}</span>
                        <span>handoff_id: {readiness.completion_evidence.handoff_id ?? 'null'}</span>
                      </div>}
                      {!readiness.completion_evidence && <span>completion evidence: none</span>}
                      {readiness.dependency_evidence.map(evidence => <div key={evidence.item_id} className="pt-1">
                        <span>dependency item_id: {evidence.item_id}</span>
                        <span>dependency status: {evidence.status}</span>
                        <span>dependency blocker_code: {evidence.blocker_code ?? 'null'}</span>
                        <span>predecessor_run_id: {evidence.predecessor_run_id ?? 'null'}</span>
                        <span>integrated_sha: {evidence.integrated_sha ?? 'null'}</span>
                        <span>handoff_id: {evidence.handoff_id ?? 'null'}</span>
                      </div>)}
                    </EvidenceDetails>}
                  </div>
                );
              })}
            </div>
          </Panel>

          <Panel
            title="Manual Work-Item Launch"
            description="Launch a saved item from this execution. The server checks readiness; child runs keep their normal plan, PR and merge gates."
          >
            {!frozenGraph ? (
              <p role="status" className="text-sm text-[var(--muted)]">
                The frozen saved graph is {workspace.loadingGraphRevisions ? 'loading' : 'unavailable'}; no item can be launched until its saved source is available.
              </p>
            ) : frozenGraph.items.length === 0 ? (
              <p className="text-sm text-[var(--muted)]">This saved graph has no work items to launch.</p>
            ) : (
              <div className="space-y-3">
                <label htmlFor="manual-work-item" className="block text-sm font-medium">Saved work item</label>
                <select
                  id="manual-work-item"
                  className="w-full max-w-2xl px-3 py-2 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
                  value={selectedItemId}
                  disabled={mutationPending}
                  onChange={event => setSelectedItemId(event.target.value)}
                >
                  <option value="">Choose an item from the frozen graph</option>
                  {frozenGraph.items.map(item => (
                    <option key={item.item_id} value={item.item_id}>
                      {item.title} · {item.disposition ?? 'required'}
                    </option>
                  ))}
                </select>
                {selectedItemId && frozenItems.get(selectedItemId) && (
                  <p className="text-sm text-[var(--muted)]">
                    {frozenItems.get(selectedItemId)?.outcome} · saved disposition: {frozenItems.get(selectedItemId)?.disposition ?? 'required'}
                  </p>
                )}
                <label className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={launchOverride}
                    disabled={mutationPending}
                    onChange={event => setLaunchOverride(event.target.checked)}
                  />
                  <span>Owner override: launch despite server-reported workflow warnings</span>
                </label>
                {launchOverride && (
                  <div className="p-3 border border-[var(--warning)] bg-[var(--warning-soft)] rounded space-y-2">
                    <p className="text-sm font-medium text-[var(--warning)]">The server will retain the saved disposition and any unresolved dependency evidence.</p>
                    <label htmlFor="manual-launch-note" className="block text-sm">Override note (optional)</label>
                    <input
                      id="manual-launch-note"
                      className="w-full max-w-2xl px-3 py-2 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
                      value={launchNote}
                      maxLength={2048}
                      disabled={mutationPending}
                      onChange={event => setLaunchNote(event.target.value)}
                    />
                  </div>
                )}
                {isWorkItemLaunchError && (
                  <div role="alert" className="p-3 border border-[var(--warning)] bg-[var(--warning-soft)] rounded space-y-2 text-sm">
                    <p>{launchBlockers.length ? 'The server blocked this launch:' : mutations.error}</p>
                    {launchBlockers.length > 0 && <ul className="list-disc pl-5">
                      {launchBlockers.map(code => <li key={code}>{blockerLabels[code] ?? `The server reported: ${code.replaceAll('_', ' ')}.`}</li>)}
                    </ul>}
                    {launchBlockers.length > 0 && <EvidenceDetails summary="Inspect server launch evidence">
                      <span>blocker_codes: {launchBlockers.join(', ')}</span>
                      <span>actual_epic_version: {mutations.errorDetail?.actual_epic_version}</span>
                    </EvidenceDetails>}
                    {!launchOverride && launchBlockers.length > 0 && <p>Choose the owner override to make a new, explicit launch request.</p>}
                  </div>
                )}
                {launchedAttempt && (
                  <div role="status" className="p-3 border border-[var(--border)] rounded space-y-2 text-sm">
                    <p>Server created attempt {launchedAttempt.attempt_number} for the saved {launchedAttempt.item_disposition} item. This is a run launch, not a completion or integration claim.</p>
                    {launchedAttempt.blocker_codes.length > 0 && <p className="text-[var(--warning)]">Warnings retained: {launchedAttempt.blocker_codes.map(code => blockerLabels[code] ?? code.replaceAll('_', ' ')).join('; ')}</p>}
                    <Link className="text-[var(--focus)] hover:underline" href={`/runs/${launchedAttempt.run_id}`}>Open launched run</Link>
                    <EvidenceDetails summary="Inspect launch receipt">
                      <span>attempt_id: {launchedAttempt.attempt_id}</span>
                      <span>run_id: {launchedAttempt.run_id}</span>
                      <span>task_id: {launchedAttempt.task_id}</span>
                      <span>execution_id: {launchedAttempt.execution_id}</span>
                    </EvidenceDetails>
                  </div>
                )}
                {workItemRuns.loading && <p role="status" className="text-sm text-[var(--muted)]">Loading saved launch history…</p>}
                {workItemRuns.failed && <p role="alert" className="text-sm text-[var(--warning)]">Saved launch history is unavailable. Retry to refresh it.</p>}
                {workItemRuns.attempts.filter(attempt => attempt.execution_id === executionSnapshot?.execution_id).length > 0 && (
                  <div className="space-y-2">
                    <h3 className="font-medium text-sm">Saved attempts in this execution</h3>
                    {workItemRuns.attempts.filter(attempt => attempt.execution_id === executionSnapshot?.execution_id).map(attempt => (
                      <div key={attempt.attempt_id} className="flex flex-wrap items-center gap-2 text-sm border-t border-[var(--border)] pt-2">
                        <span>Attempt {attempt.attempt_number} · {frozenItems.get(attempt.item_id)?.title ?? 'Saved work item'} · {attempt.item_disposition}</span>
                        <Link className="text-[var(--focus)] hover:underline" href={`/runs/${attempt.run_id}`}>Open run</Link>
                        {attempt.blocker_codes.length > 0 && <span className="text-[var(--warning)]">Warnings: {attempt.blocker_codes.map(code => blockerLabels[code] ?? code.replaceAll('_', ' ')).join(', ')}</span>}
                      </div>
                    ))}
                  </div>
                )}
                <Button
                  variant="primary"
                  disabled={!selectedItemId || mutationPending || !executionSnapshot || executionSnapshot.epic_id !== epicId}
                  onClick={() => { void launchWorkItem(); }}
                >Launch Work Item</Button>
              </div>
            )}
          </Panel>

          {/* Shared Epic Budget Panel */}
          <EpicBudgetPanel epicId={epicId} />
        </div>
      )}
    </div>
  );
}
