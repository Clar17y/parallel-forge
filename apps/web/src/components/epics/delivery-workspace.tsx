'use client';

import { useEffect, useState, type FormEvent } from 'react';
import Link from 'next/link';
import { Button } from '@/components/ui/button';
import { Panel } from '@/components/ui/panel';
import { StatusBadge } from '@/components/ui/status-badge';
import { useEpicExecution } from '@/hooks/epics/use-epic-execution';
import { useEpicWorkspace } from '@/hooks/epics/use-epic-workspace';
import { EvidenceDetails } from './evidence-details';

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

  const {
    executionId,
    setExecutionId,
    execution,
    loading,
    failed,
    refresh,
    isPendingDiscovery,
    state,
    childRuns,
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
        const label = action[0].toUpperCase() + action.slice(1);
        setActionNotice(`${label} requested; waiting for server confirmation.`);
      }
    });
    return () => { unregisterStart(); unregisterCommand(); };
  }, [registerCompletion]);

  const handleStart = async () => {
    try {
      await startExecution(epicVersion);
    } catch {
      // Mutations hook captures errors
    }
  };

  const handleCommand = async (action: 'pause' | 'resume' | 'cancel') => {
    if (!execution) return;
    try {
      await sendCommand(action, execution.execution_version);
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

  const frozenGraph = execution
    ? workspace.graphRevisions.find(revision => revision.graph_revision_id === execution.graph_revision_id)
    : undefined;
  const frozenItems = new Map((frozenGraph?.items ?? []).map(item => [item.item_id, item]));
  const mutationPending = mutations.loading || mutations.hasPendingRetry;
  const isDeliveryAction = !mutations.actionKind || ['execution-start', 'execution-command'].includes(mutations.actionKind);

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

      {/* Discovery Pending Limits Disclosure */}
      {isPendingDiscovery && (
        <Panel
          title="Execution Discovery"
          description="Load a known execution or start one from the current accepted graph."
        >
          <div className="space-y-4">
            <div className="p-4 bg-[var(--surface-muted)] border border-[var(--border)] rounded text-sm text-[var(--muted)] space-y-2">
              <p>
                Execution discovery is unavailable until an execution ID is supplied or returned by a start request.
              </p>
            </div>

            <form onSubmit={handleManualIdSubmit} className="flex flex-col sm:flex-row gap-2">
              <label className="sr-only" htmlFor="execution-id">Execution ID</label><input
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
            </form>
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
            description={`Execution Version ${execution.execution_version} • Bound to Epic Version ${execution.epic_version}`}
            className="[&_.panel-heading]:flex-col [&_.panel-heading]:items-start"
            action={undefined}
          >
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge
                  label={state?.replaceAll('_', ' ') ?? 'Unknown'}
                  tone={
                    state === 'SUCCEEDED'
                      ? 'success'
                      : state === 'BLOCKED' || state === 'CANCELLED'
                      ? 'danger'
                      : state === 'PAUSED' || state?.includes('REQUESTED')
                      ? 'warning'
                      : 'info'
                  }
                />
              </div>
              <EvidenceDetails summary="Inspect Execution ID">
                <span>execution_id: {execution.execution_id}</span>
              </EvidenceDetails>
              <EvidenceDetails summary="Inspect frozen brief and graph revisions">
                <span>brief_revision_id: {execution.brief_revision_id}</span>
                <span>graph_revision_id: {execution.graph_revision_id}</span>
              </EvidenceDetails>

              {/* Verified Success Display */}
              {state === 'SUCCEEDED' && (
                <div role="status" className="p-4 bg-[var(--success-soft)] border border-[var(--success)] rounded text-sm space-y-2">
                  <p className="font-semibold text-[var(--success)]">
                    ✓ Overall Verified Success: Validated from server state and merge/completion evidence.
                  </p>
                </div>
              )}

              {/* Execution Controls */}
              <div className="pt-2 flex flex-wrap gap-2 border-t border-[var(--border)]">
                <Button
                  variant="secondary"
                  disabled={mutationPending || state !== 'ACTIVE'}
                  onClick={() => handleCommand('pause')}
                >
                  Pause Execution
                </Button>
                <Button
                  variant="secondary"
                  disabled={mutationPending || state !== 'PAUSED'}
                  onClick={() => handleCommand('resume')}
                >
                  Resume Execution
                </Button>
                <Button
                  variant="danger"
                  disabled={mutationPending || !['ACTIVE', 'PAUSED', 'BLOCKED'].includes(state ?? '')}
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
              <p className="text-sm">{state === 'SUCCEEDED' ? 'All required work is verified complete.' : state === 'BLOCKED' ? 'Resolve the reported blockers before continuing.' : state === 'CANCELLED' ? 'The execution is cancelled.' : state?.includes('REQUESTED') ? 'The requested change is still being processed.' : state === 'PAUSED' ? 'The execution is paused. Resume it when ready; any active child gate remains in place.' : execution.active_child?.pending_gate ? `Review the ${execution.active_child.pending_gate} gate for the active child run.` : 'The server is evaluating the next eligible work item.'}</p>
            </Panel>

            <Panel title="Active Blockers">
              {!execution.items.some(item => item.blocker_code) ? (
                <p className="text-sm text-[var(--muted)]">No active blockers.</p>
              ) : (
                <ul className="list-disc pl-5 text-sm text-[var(--danger)] space-y-1">
                  {execution.items.filter(item => item.blocker_code).map(item => (
                    <li key={item.item_id}>{item.blocker_code}</li>
                  ))}
                </ul>
              )}
            </Panel>
          </div>

          {/* Child Runs & Approval Gates */}
          <Panel
            title="Linked Child Runs & Human Approval Gates"
            description="Child runs executed within Forge retain independent review and human approval boundaries."
          >
            {childRuns.length === 0 ? (
              <p className="text-sm text-[var(--muted)]">No child run is currently active.</p>
            ) : (
              <div className="space-y-3">
                {childRuns.map(run => (
                  <div
                    key={run.run_id}
                    className="p-3 border border-[var(--border)] rounded bg-[var(--surface)] flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-sm"
                  >
                    <div>
                      <div className="flex flex-wrap items-center gap-2 min-w-0">
                        <Link href={`/runs/${run.run_id}`} className="min-w-0 break-words font-medium text-[var(--focus)] hover:underline">
                          Run for {frozenItems.get(run.item_id)?.title ?? 'active work item'}
                        </Link>
                        <StatusBadge label={run.run_state.replaceAll('_', ' ')} tone="neutral" />
                        {run.pending_gate && (
                          <StatusBadge label={run.pending_gate} tone="warning" />
                        )}
                      </div>
                      <p className="text-xs text-[var(--muted)] break-words">{frozenItems.get(run.item_id)?.title ?? 'Work item'} · run version {run.run_version}</p>
                      <EvidenceDetails summary="Inspect run and work item IDs"><span>run_id: {run.run_id}</span><span>item_id: {run.item_id}</span></EvidenceDetails>
                      {run.pending_evidence_digest && <EvidenceDetails summary="Inspect pending gate evidence"><span>evidence_digest: {run.pending_evidence_digest}</span></EvidenceDetails>}
                    </div>

                    {run.pending_gate && (
                      <Link
                        href={`/runs/${run.run_id}`}
                        className="button text-xs self-start shrink-0 sm:self-auto"
                        data-variant="secondary"
                      >
                        Review Gate at {run.pending_gate}
                      </Link>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Panel>

          {/* Item Progression */}
          <Panel title="Work-Item Progression">
            {!frozenGraph && <p role="status" className="mb-3 text-sm text-[var(--muted)]">Details for this frozen graph are {workspace.loadingGraphRevisions ? 'loading' : 'unavailable'}.</p>}
            <div className="space-y-2">
              {(execution.items ?? []).map((it, idx) => (
                <div key={it.item_id} className="p-3 border border-[var(--border)] rounded text-sm flex flex-col sm:flex-row sm:items-start sm:justify-between gap-2 min-w-0 break-words">
                  <div className="flex flex-wrap items-center gap-2 min-w-0">
                    <span className="font-semibold">#{idx + 1} {frozenItems.get(it.item_id)?.title ?? 'Work item details unavailable'}</span>
                    <StatusBadge label={it.disposition} tone={it.disposition === 'deferred' ? 'warning' : 'neutral'} />
                    <StatusBadge label={it.status} tone={it.status === 'succeeded' || it.status === 'satisfied' ? 'success' : it.status === 'active' ? 'info' : it.status === 'blocked' ? 'danger' : 'neutral'} />
                  </div>
                  {it.run_id && (
                    <Link href={`/runs/${it.run_id}`} className="text-xs text-[var(--focus)] hover:underline">
                      Open run
                    </Link>
                  )}
                  {it.blocker_code && <p className="text-xs text-[var(--danger)]">Blocker: {it.blocker_code}</p>}
                  <EvidenceDetails summary="Inspect work item identity and digest"><span>item_id: {it.item_id}</span>{frozenItems.get(it.item_id)?.item_digest && <span>item_digest: {frozenItems.get(it.item_id)?.item_digest}</span>}</EvidenceDetails>
                </div>
              ))}
            </div>
          </Panel>

          {/* Resource Usage & Unknown Dimensions */}
          <Panel
            title="Execution Usage & Resource Dimensions"
            description="Known, reserved, and explicit unknown dimensions (unknown is never zero)."
          >
            <div className="space-y-3 text-sm">
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                <div className="p-3 bg-[var(--surface-muted)] rounded">
                  <span className="text-xs text-[var(--muted)] block">Known cost</span><span className="font-semibold">{execution.aggregate_usage.known_cost_minor} minor units</span>
                </div>
                <div className="p-3 bg-[var(--surface-muted)] rounded">
                  <span className="text-xs text-[var(--muted)] block">Reserved cost</span><span className="font-semibold">{execution.aggregate_usage.reserved_cost_minor} minor units</span>
                </div>
              </div>

              <p className="text-xs text-[var(--muted)]">{execution.aggregate_usage.unknown_usage ? 'Some usage is unknown. Unknown usage is not zero.' : 'All reported usage is accounted for.'}</p>
            </div>
          </Panel>
        </div>
      )}
    </div>
  );
}
