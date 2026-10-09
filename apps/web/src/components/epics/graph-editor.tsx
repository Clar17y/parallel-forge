'use client';

import { LoadingStatus } from '@/components/ui/loading-status';
import { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Panel } from '@/components/ui/panel';
import { StatusBadge } from '@/components/ui/status-badge';
import { useEpicWorkspace } from '@/hooks/epics/use-epic-workspace';
import { EvidenceDetails } from './evidence-details';
import type { ItemInput, ItemReadiness } from '@/hooks/epics/types';

export interface GraphProposal {
  schema_version: 1;
  brief_revision_id: string;
  brief_digest: string;
  proposal_digest: string;
  items: ItemInput[];
}

export function GraphEditor({ epicId, proposedGraph }: { epicId: string; proposedGraph?: GraphProposal }) {
  const workspace = useEpicWorkspace(epicId);
  const [viewTab, setViewTab] = useState<'editor' | 'accepted' | 'history'>('editor');
  const [saveSuccess, setSaveSuccess] = useState<string | null>(null);

  const {
    epic,
    loading,
    failed,
    refreshProjections,
    acceptedBrief,
    acceptedGraph,
    graphRevisions,
    loadingGraphRevisions,
    failedGraphRevisions,
    refreshGraphRevisions,
    loadingAcceptedBrief,
    failedAcceptedBrief,
    loadingAcceptedGraph,
    saveGraphRevision,
    adoptGraph,
    mutations,
  } = workspace;

  const matchingRevision = graphRevisions.filter(revision =>
    revision.brief_revision_id === epic?.accepted_brief_revision_id &&
    revision.brief_digest === epic?.accepted_brief_digest).at(-1);
  const baseline = matchingRevision ?? acceptedGraph;
  const initialItems: ItemInput[] = (baseline?.items ?? []).map(it => ({
    item_id: it.item_id, title: it.title, outcome: it.outcome,
    disposition: it.disposition, ordinal: it.ordinal,
    source_requirement_ids: it.source_requirement_ids ?? [],
    dependency_item_ids: it.dependency_item_ids ?? [],
    acceptance_criteria: it.acceptance_criteria ?? [],
  }));

  const [userItems, setUserItems] = useState<ItemInput[] | null>(null);
  const [editBase, setEditBase] = useState<{ version: number; briefId: string | null; briefDigest: string | null } | null>(null);
  const userItemsRef = useRef(userItems);
  useEffect(() => { userItemsRef.current = userItems; }, [userItems]);
  const { registerCompletion } = mutations;
  const isGraphAction = !mutations.actionKind || ['graph-revision', 'graph-adoption'].includes(mutations.actionKind);
  const items = userItems ?? initialItems;
  useEffect(() => registerCompletion('graph-revision', (value, request) => {
    setSaveSuccess('Graph revision #' + (value as { revision_number: number }).revision_number + ' saved.');
    const submitted = request.body.items;
    const unchanged = JSON.stringify(userItemsRef.current) === JSON.stringify(submitted);
    if (unchanged) {
      setUserItems(null);
      setEditBase(null);
    } else {
      setEditBase(previous => previous ? { ...previous, version: (value as { epic_version?: number }).epic_version ?? previous.version + 1 } : previous);
    }
  }), [registerCompletion]);
  useEffect(() => registerCompletion('graph-adoption', () => {
    setSaveSuccess('Graph adopted as the accepted baseline.');
  }), [registerCompletion]);
  const statusRef = useRef<HTMLDivElement>(null);
  useEffect(() => { if (saveSuccess) statusRef.current?.focus(); }, [saveSuccess]);
  const serverChanged = editBase !== null && !!epic && (epic.version !== editBase.version || epic.accepted_brief_revision_id !== editBase.briefId || epic.accepted_brief_digest !== editBase.briefDigest);
  const editItems = (next: ItemInput[]) => {
    if (!editBase) setEditBase({ version: epic?.version ?? 1, briefId: epic?.accepted_brief_revision_id ?? null, briefDigest: epic?.accepted_brief_digest ?? null });
    setUserItems(next);
  };

  if (loading && !epic) {
    return <LoadingStatus>Loading epic graph workspace…</LoadingStatus>;
  }

  if (failed && !epic) {
    return (
      <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)]">
        <p>Failed to load work-item graph workspace.</p>
        <Button onClick={refreshProjections} className="mt-2">
          Retry
        </Button>
      </div>
    );
  }

  const availableRequirements = acceptedBrief?.requirements ?? [];
  const incompleteItems = items.some(item => !item.title.trim() || !item.outcome.trim() || !item.acceptance_criteria.length || item.acceptance_criteria.some(criterion => !criterion.trim()) || !item.source_requirement_ids.length);

  const addItem = () => {
    const newItem: ItemInput = {
      item_id: crypto.randomUUID(),
      title: '',
      outcome: '',
      disposition: 'required',
      ordinal: items.length,
      source_requirement_ids: [],
      dependency_item_ids: [],
      acceptance_criteria: [''],
    };
    editItems([...items, newItem]);
  };

  const updateItem = (index: number, patch: Partial<ItemInput>) => {
    const updated = [...items];
    updated[index] = { ...updated[index], ...patch };
    editItems(updated);
  };

  const removeItem = (index: number) => {
    const removedId = items[index].item_id;
    editItems(items.filter((_, i) => i !== index).map((item, i) => ({
      ...item, ordinal: i, dependency_item_ids: (item.dependency_item_ids ?? []).filter(id => id !== removedId),
    })));
  };

  const moveItem = (index: number, direction: 'up' | 'down') => {
    const targetIndex = direction === 'up' ? index - 1 : index + 1;
    if (targetIndex < 0 || targetIndex >= items.length) return;
    const updated = [...items];
    const temp = updated[index];
    updated[index] = updated[targetIndex];
    updated[targetIndex] = temp;
    editItems(updated.map((item, i) => ({ ...item, ordinal: i })));
  };

  const handleSaveRevision = async () => {
    if (!acceptedBrief?.brief_revision_id || !acceptedBrief?.brief_digest || serverChanged) return;
    setSaveSuccess(null);
    try {
      await saveGraphRevision(
        editBase?.briefId ?? acceptedBrief.brief_revision_id,
        editBase?.briefDigest ?? acceptedBrief.brief_digest,
        items,
        editBase?.version ?? epic?.version
      );
    } catch {
      // Mutations hook captures error
    }
  };

  const handleAdoptGraph = async (revId: string, digest: string) => {
    setSaveSuccess(null);
    try {
      await adoptGraph(revId, digest);
    } catch {
      // Mutations hook captures error
    }
  };

  const readinessMap = new Map<string, ItemReadiness>();
  (baseline?.readiness ?? []).forEach(r => {
    readinessMap.set(r.item_id, r);
  });

  return (
    <div className="graph-workspace space-y-6">
      {/* Navigation tabs */}
      <nav aria-label="Graph views" className="flex space-x-2 border-b border-[var(--border)] pb-2">
        <Button
          variant={viewTab === 'editor' ? 'primary' : 'quiet'}
          aria-current={viewTab === 'editor' ? 'page' : undefined}
          onClick={() => setViewTab('editor')}
        >
          Graph Editor
        </Button>
        <Button
          variant={viewTab === 'accepted' ? 'primary' : 'quiet'}
          aria-current={viewTab === 'accepted' ? 'page' : undefined}
          onClick={() => setViewTab('accepted')}
        >
          Accepted Graph
        </Button>
        <Button
          variant={viewTab === 'history' ? 'primary' : 'quiet'}
          aria-current={viewTab === 'history' ? 'page' : undefined}
          onClick={() => setViewTab('history')}
        >
          Graph Revisions ({graphRevisions.length})
        </Button>
      </nav>

      {/* Uncertainty Retry Banner */}
      {mutations.hasPendingRetry && !mutations.loading && !mutations.shared && (
        <div role="alert" className="p-4 bg-[var(--warning-soft)] text-[var(--warning)] rounded border border-[var(--border)] space-y-2">
          <p className="font-semibold">Network or server error. Mutation outcome uncertain.</p>
          <div className="flex space-x-2">
            <Button variant="primary" onClick={() => { void mutations.retryPending().catch(() => undefined); }}>
              Retry original request
            </Button>
          </div>
        </div>
      )}

      {mutations.error && !mutations.conflict && !mutations.hasPendingRetry && isGraphAction && <p role="alert">{mutations.error}</p>}
      {/* Stale CAS Conflict Banner */}
      {mutations.conflict && isGraphAction && (
        <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)] space-y-2">
          <p className="font-semibold">Epic version conflict: The epic version changed on the server before saving.</p>
          <Button variant="secondary" onClick={() => { mutations.clearError(); refreshProjections(); }}>
            Refresh projections
          </Button>
        </div>
      )}
      {serverChanged && <div role="alert" className="p-4 border rounded space-y-2"><p>The epic version changed on the server. Compare the latest graph below before replacing it.</p><details><summary>Inspect current server graph</summary><pre className="overflow-auto text-xs">{JSON.stringify(initialItems, null, 2)}</pre></details><Button variant="secondary" onClick={() => { setEditBase({ version: epic?.version ?? 1, briefId: epic?.accepted_brief_revision_id ?? null, briefDigest: epic?.accepted_brief_digest ?? null }); }}>Use my edited graph against this version</Button><Button variant="quiet" onClick={() => { setUserItems(null); setEditBase(null); }}>Load server graph</Button></div>}

      {proposedGraph && <Panel title="Graph proposal" description="Proposed items have not been adopted. Copy them into the editor, then save and choose a revision explicitly.">
        <ul className="space-y-2">{proposedGraph.items.map(item => <li key={item.item_id}><strong>{item.title}</strong> · {item.disposition}<p>{item.outcome}</p><ul>{item.acceptance_criteria.map((criterion, index) => <li key={index}>{criterion}</li>)}</ul></li>)}</ul>
        <EvidenceDetails summary="Inspect proposal binding"><p>proposal_digest: {proposedGraph.proposal_digest}</p><p>brief_revision_id: {proposedGraph.brief_revision_id}</p><p>brief_digest: {proposedGraph.brief_digest}</p></EvidenceDetails>
        {proposedGraph.brief_revision_id !== epic?.accepted_brief_revision_id || proposedGraph.brief_digest !== epic?.accepted_brief_digest ? <p role="alert">This proposal uses a different brief. Keep it for inspection and use a proposal for the current accepted brief.</p> : null}
        <Button variant="secondary" disabled={mutations.loading || mutations.hasPendingRetry || userItems !== null || proposedGraph.brief_revision_id !== epic?.accepted_brief_revision_id || proposedGraph.brief_digest !== epic?.accepted_brief_digest} onClick={() => { setEditBase({ version: epic?.version ?? 1, briefId: proposedGraph.brief_revision_id, briefDigest: proposedGraph.brief_digest }); setUserItems(proposedGraph.items.map(item => ({ ...item }))); setViewTab('editor'); }}>Edit proposed items</Button>
      </Panel>}
      {/* Missing Accepted Brief Warning */}
      {!epic?.accepted_brief_revision_id && (
        <div role="alert" className="p-4 bg-[var(--warning-soft)] text-[var(--warning)] rounded border border-[var(--border)] space-y-1">
          <p className="font-semibold">No Accepted Brief Baseline</p>
          <p className="text-sm">
            Work-item graphs must be bound to an accepted requirements brief revision and digest. Please accept a brief in the Brief Editor before saving or adopting a graph.
          </p>
        </div>
      )}
      {epic?.accepted_brief_revision_id && failedAcceptedBrief && <div role="alert" className="space-y-2">
        <p>The accepted brief could not be loaded.</p>
        <Button variant="secondary" onClick={refreshProjections}>Retry accepted brief</Button>
      </div>}

      {/* Structural Readiness Disclaimer */}
      <div className="p-3 bg-[var(--surface-muted)] text-[var(--muted)] text-xs rounded border border-[var(--border)]">
        <strong>Notice:</strong> Structural readiness is verified by graph constraints; execution admission is managed separately under Delivery. Graph editing does not hot-swap an active frozen execution.
      </div>

      {saveSuccess && (
        <div role="status" ref={statusRef} tabIndex={-1} className="p-3 bg-[var(--success-soft)] text-[var(--success)] rounded text-sm">
          {saveSuccess}
        </div>
      )}

      {/* VIEW: GRAPH EDITOR */}
      {viewTab === 'editor' && (
        <fieldset disabled={mutations.loading || mutations.hasPendingRetry} className="space-y-6 min-w-0">
          <legend className="sr-only">Work-item graph editor</legend>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <h2 className="text-lg font-semibold">Work-Item Decomposition</h2>
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="secondary" onClick={addItem}>
                + Add Item
              </Button>
              <Button
                type="button"
                variant="primary"
                disabled={mutations.loading || mutations.hasPendingRetry || serverChanged || incompleteItems || !acceptedBrief?.brief_revision_id || items.length === 0}
                onClick={handleSaveRevision}
              >
                {mutations.loading ? 'Saving…' : 'Save new graph revision'}
              </Button>
            </div>
          </div>

          {incompleteItems && <p role="status">Complete each item’s title, outcome, acceptance criteria and source requirements before saving.</p>}
          {items.length === 0 ? (
            <p className="text-sm text-[var(--muted)]">No work items in this graph yet. Click &quot;+ Add Item&quot; to decompose the requirements.</p>
          ) : (
            <div className="space-y-4">
              {items.map((item, idx) => {
                const readiness = readinessMap.get(item.item_id);
                return (
                  <div
                    key={item.item_id}
                    role="group" aria-label={'Work item ' + (idx + 1)}
                    className="p-4 border border-[var(--border)] rounded bg-[var(--surface)] space-y-4"
                  >
                    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                      <div className="flex items-center space-x-2">
                        <span className="font-semibold text-sm">Item #{idx + 1}</span>
                        <StatusBadge
                          label={item.disposition === 'required' ? 'Required' : 'Deferred'}
                          tone={item.disposition === 'required' ? 'neutral' : 'warning'}
                        />
                        {readiness && (
                          <StatusBadge
                            label={readiness.status}
                            tone={readiness.status === 'ready' ? 'success' : readiness.status === 'blocked' ? 'danger' : 'neutral'}
                          />
                        )}
                      </div>
                      <div className="flex items-center space-x-1">
                        <Button
                          variant="quiet"
                          disabled={idx === 0}
                          onClick={() => moveItem(idx, 'up')}
                          aria-label={`Move item ${idx + 1} up`}
                        >
                          ↑
                        </Button>
                        <Button
                          variant="quiet"
                          disabled={idx === items.length - 1}
                          onClick={() => moveItem(idx, 'down')}
                          aria-label={`Move item ${idx + 1} down`}
                        >
                          ↓
                        </Button>
                        <Button
                          variant="quiet"
                          className="text-[var(--danger)] text-sm"
                          onClick={() => removeItem(idx)}
                        >
                          Remove
                        </Button>
                      </div>
                    </div>

                    <div>
                      <span className="block text-xs font-medium mb-1">Item acceptance criteria</span>
                      {item.acceptance_criteria.map((criterion, criterionIndex) => <div key={criterionIndex} className="flex gap-2 mb-2"><input aria-label={`Criterion ${criterionIndex + 1} for item ${idx + 1}`} className="flex-1 px-3 py-1.5 border rounded" value={criterion} onChange={event => updateItem(idx, { acceptance_criteria: item.acceptance_criteria.map((value, index) => index === criterionIndex ? event.target.value : value) })} /><Button type="button" variant="quiet" aria-label={`Remove criterion ${criterionIndex + 1} for item ${idx + 1}`} onClick={() => updateItem(idx, { acceptance_criteria: item.acceptance_criteria.filter((_, index) => index !== criterionIndex) })}>Remove</Button></div>)}
                      <Button type="button" variant="quiet" onClick={() => updateItem(idx, { acceptance_criteria: [...item.acceptance_criteria, ''] })}>Add criterion</Button>
                    </div>

                    <EvidenceDetails summary="Inspect item ID">
                      <span>item_id: {item.item_id}</span>
                    </EvidenceDetails>

                    {readiness && (
                      <p className="text-xs text-[var(--muted)] bg-[var(--surface-muted)] p-2 rounded">
                        <strong>Readiness projection:</strong> {readiness.reason}
                      </p>
                    )}

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      <div>
                        <label htmlFor={`item-title-${item.item_id}`} className="block text-xs font-medium text-[var(--muted)] mb-1">
                          Item title
                        </label>
                        <input
                          id={`item-title-${item.item_id}`}
                          aria-label={`Item title ${idx + 1}`}
                          type="text"
                          className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
                          maxLength={256}
                          value={item.title}
                          onChange={e => updateItem(idx, { title: e.target.value })}
                          placeholder="Task title..."
                        />
                      </div>

                      <div>
                        <label htmlFor={`item-outcome-${item.item_id}`} className="block text-xs font-medium text-[var(--muted)] mb-1">
                          Outcome / deliverable
                        </label>
                        <input
                          id={`item-outcome-${item.item_id}`}
                          aria-label={`Item outcome ${idx + 1}`}
                          type="text"
                          className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
                          maxLength={5000}
                          value={item.outcome}
                          onChange={e => updateItem(idx, { outcome: e.target.value })}
                          placeholder="Concrete outcome..."
                        />
                      </div>
                    </div>

                    <fieldset className="flex items-center space-x-4 text-sm"><legend className="text-xs">Disposition for item {idx + 1}</legend>
                      <label className="flex items-center space-x-2">
                        <input
                          type="radio"
                          name={`disposition-${item.item_id}`}
                          checked={item.disposition === 'required'}
                          onChange={() => updateItem(idx, { disposition: 'required' })}
                        />
                        <span>Required</span>
                      </label>
                      <label className="flex items-center space-x-2">
                        <input
                          type="radio"
                          name={`disposition-${item.item_id}`}
                          checked={item.disposition === 'deferred'}
                          onChange={() => updateItem(idx, { disposition: 'deferred' })}
                        />
                        <span>Deferred</span>
                      </label>
                    </fieldset>

                    {/* Source Requirements Selection */}
                    <div className="space-y-1">
                      <span className="block text-xs font-medium text-[var(--muted)]">
                        Bound Source Requirements (from Accepted Brief):
                      </span>
                      {!acceptedBrief && epic?.accepted_brief_revision_id ? (
                        <p role="status" className="text-xs text-[var(--muted)]">{loadingAcceptedBrief ? 'Loading the accepted brief…' : 'The accepted brief could not be loaded. Refresh the workspace to retry.'}</p>
                      ) : availableRequirements.length === 0 ? (
                        <p className="text-xs text-[var(--muted)] italic">No requirements in accepted brief.</p>
                      ) : (
                        <div className="flex flex-wrap gap-2 pt-1">
                          {availableRequirements.map(req => {
                            const isSelected = item.source_requirement_ids.includes(req.requirement_id);
                            return (
                              <button
                                key={req.requirement_id}
                                type="button"
                                className={`text-xs px-2.5 py-1 rounded border ${
                                  isSelected
                                    ? 'bg-[var(--accent)] text-white border-[var(--accent)]'
                                    : 'bg-[var(--surface)] text-[var(--muted)] border-[var(--border)] hover:border-[var(--control-border)]'
                                }`}
                                aria-pressed={isSelected}
                                onClick={() => {
                                  const updated = isSelected
                                    ? item.source_requirement_ids.filter(id => id !== req.requirement_id)
                                    : [...item.source_requirement_ids, req.requirement_id];
                                  updateItem(idx, { source_requirement_ids: updated });
                                }}
                              >
                                {isSelected ? '✓ ' : '+ '}
                                {req.text.slice(0, 30)}...
                              </button>
                            );
                          })}
                        </div>
                      )}
                    </div>

                    {/* Dependencies Selection */}
                    <div className="space-y-1">
                      <span className="block text-xs font-medium text-[var(--muted)]">
                        Dependencies (items this item depends on):
                      </span>
                      <div className="flex flex-wrap gap-2 pt-1">
                        {items
                          .filter(other => other.item_id !== item.item_id)
                          .map(other => {
                            const isDep = item.dependency_item_ids?.includes(other.item_id);
                            return (
                              <button
                                key={other.item_id}
                                type="button"
                                className={`text-xs px-2.5 py-1 rounded border ${
                                  isDep
                                    ? 'bg-[var(--info)] text-white border-[var(--info)]'
                                    : 'bg-[var(--surface)] text-[var(--muted)] border-[var(--border)]'
                                }`}
                                aria-pressed={Boolean(isDep)}
                                onClick={() => {
                                  const currentDeps = item.dependency_item_ids ?? [];
                                  const updated = isDep
                                    ? currentDeps.filter(id => id !== other.item_id)
                                    : [...currentDeps, other.item_id];
                                  updateItem(idx, { dependency_item_ids: updated });
                                }}
                              >
                                {isDep ? '✓ Depends on: ' : '+ Dep: '}
                                {other.title || 'Untitled item'}
                              </button>
                            );
                          })}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </fieldset>
      )}

      {/* VIEW: ACCEPTED GRAPH */}
      {viewTab === 'accepted' && (
        <Panel
          title="Accepted Graph Projection"
          description="The active graph baseline accepted for execution planning. Updating the brief clears this selection."
          action={
            epic?.accepted_graph_revision_id ? (
              <StatusBadge label="Adopted Graph" tone="success" />
            ) : (
              <StatusBadge label="No Graph Adopted" tone="neutral" />
            )
          }
        >
          {!acceptedGraph ? (
            <div className="space-y-2">
              <p className="text-sm text-[var(--muted)]">{epic?.accepted_graph_revision_id ? loadingAcceptedGraph ? 'Loading the accepted graph…' : 'The accepted graph could not be loaded. Refresh the workspace to retry.' : 'No graph has been accepted yet.'}</p>
              {epic?.accepted_graph_revision_id && !loadingAcceptedGraph && <Button variant="secondary" onClick={refreshProjections}>Retry accepted graph</Button>}
            </div>
          ) : (
            <div className="space-y-4">
              <div className="p-3 bg-[var(--surface-muted)] rounded space-y-1 text-sm">
                <p>Accepted work-item baseline</p>
                <EvidenceDetails summary="Inspect digests">
                  <p>graph_digest: {acceptedGraph.graph_digest}</p>
                  <p>brief_revision_id: {acceptedGraph.brief_revision_id}</p>
                  <p>brief_digest: {acceptedGraph.brief_digest}</p>
                  <pre className="overflow-auto">{JSON.stringify(acceptedGraph.items, null, 2)}</pre>
                </EvidenceDetails>
              </div>

              <div className="space-y-2">
                {acceptedGraph.items.map((it, idx) => (
                  <div key={it.item_id} className="p-3 border border-[var(--border)] rounded text-sm space-y-1">
                    <div className="flex items-center space-x-2">
                      <span className="font-semibold">#{idx + 1} {it.title}</span>
                      <StatusBadge label={it.disposition} tone={it.disposition === 'required' ? 'neutral' : 'warning'} />
                    </div>
                    <p className="text-xs text-[var(--muted)]">Outcome: {it.outcome}</p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Panel>
      )}

      {/* VIEW: REVISION HISTORY */}
      {viewTab === 'history' && (
        <Panel
          title="Graph Revision History"
          description="Saved graph revisions. Adopting a revision validates that its bound brief matches current accepted brief."
        >
          {loadingGraphRevisions && graphRevisions.length === 0 && <LoadingStatus>Loading graph history…</LoadingStatus>}
          {failedGraphRevisions && <div role="alert" className="space-y-2">
            <p>Graph history could not be loaded.</p>
            <Button variant="secondary" onClick={refreshGraphRevisions}>Retry graph history</Button>
          </div>}
          {!loadingGraphRevisions && !failedGraphRevisions && graphRevisions.length === 0 && (
            <p className="text-sm text-[var(--muted)]">No graph revisions have been saved yet.</p>
          )}
          {graphRevisions.length > 0 && (
            <div className="space-y-3">
              {graphRevisions.map(rev => {
                const isAccepted = rev.graph_revision_id === epic?.accepted_graph_revision_id;
                return (
                  <div
                    key={rev.graph_revision_id}
                    className="p-4 border border-[var(--border)] rounded bg-[var(--surface)] flex flex-col md:flex-row md:items-center justify-between gap-4"
                  >
                    <div className="space-y-1">
                      <div className="flex items-center space-x-2">
                        <span className="font-semibold text-sm">Graph Revision #{rev.revision_number}</span>
                        {isAccepted && <StatusBadge label="Currently Accepted" tone="success" />}
                        <span className="text-xs text-[var(--muted)]">
                          {new Date(rev.created_at).toLocaleString()}
                        </span>
                      </div>
                      <p className="text-xs text-[var(--muted)]">{rev.items?.length ?? 0} items</p>
                      <EvidenceDetails summary="Inspect IDs & digests">
                        <p>graph_revision_id: {rev.graph_revision_id}</p>
                        <p>graph_digest: {rev.graph_digest}</p>
                        <p>brief_revision_id: {rev.brief_revision_id}</p>
                        <p>brief_digest: {rev.brief_digest}</p>
                        <pre className="overflow-auto">{JSON.stringify(rev.items, null, 2)}</pre>
                      </EvidenceDetails>
                    </div>

                    <div>
                      {!isAccepted && (
                        <Button
                          variant="secondary"
                          disabled={mutations.loading || mutations.hasPendingRetry || rev.brief_revision_id !== epic?.accepted_brief_revision_id || rev.brief_digest !== epic?.accepted_brief_digest}
                          onClick={() => handleAdoptGraph(rev.graph_revision_id, rev.graph_digest)}
                        >
                          Adopt revision #{rev.revision_number}
                        </Button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </Panel>
      )}
    </div>
  );
}
