'use client';

import { LoadingStatus } from '@/components/ui/loading-status';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Button } from '@/components/ui/button';
import { Panel } from '@/components/ui/panel';
import { StatusBadge } from '@/components/ui/status-badge';
import { useEpicWorkspace } from '@/hooks/epics/use-epic-workspace';
import { EvidenceDetails } from './evidence-details';
import type { BriefRequirement } from '@/hooks/epics/types';

export type BriefListField = 'outcomes' | 'scope' | 'exclusions' | 'decisions' | 'assumptions' | 'open_questions';

function getBriefItemLabel(field: BriefListField, idx: number): string {
  switch (field) {
    case 'outcomes':
      return `Outcome ${idx + 1}`;
    case 'scope':
      return `Scope item ${idx + 1}`;
    case 'exclusions':
      return `Exclusion item ${idx + 1}`;
    case 'decisions':
      return `Decision ${idx + 1}`;
    case 'assumptions':
      return `Assumption ${idx + 1}`;
    case 'open_questions':
      return `Open question ${idx + 1}`;
  }
}

interface BriefListRowsProps {
  rows: string[];
  field: BriefListField;
  inputClassName?: string;
  update: (index: number, value: string) => void;
  remove: (index: number) => void;
}

function BriefListRows({
  rows,
  field,
  inputClassName = 'flex-1 px-2 py-1 text-sm border border-[var(--control-border)] rounded',
  update,
  remove,
}: BriefListRowsProps) {
  return (
    <div className="space-y-2">
      {rows.map((item, idx) => (
        <div key={idx} className="flex items-center space-x-2">
          <input
            type="text"
            aria-label={getBriefItemLabel(field, idx)}
            className={inputClassName}
            value={item}
            onChange={e => update(idx, e.target.value)}
          />
          <Button type="button" variant="quiet" onClick={() => remove(idx)}>
            ×
          </Button>
        </div>
      ))}
    </div>
  );
}

export function BriefEditor({ epicId, onBrainstorm }: { epicId: string; onBrainstorm?: () => void }) {
  const workspace = useEpicWorkspace(epicId);
  const [viewTab, setViewTab] = useState<'editor' | 'accepted' | 'history'>('editor');
  const [saveSuccess, setSaveSuccess] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const {
    epic,
    loading,
    failed,
    refreshEpic,
    refreshProjections,
    draftTitle,
    updateDraftTitle,
    draftContent,
    updateDraftProblem,
    updateDraftContent,
    addRequirement,
    updateRequirement,
    removeRequirement,
    isDirty,
    hasServerConflict,
    revertLocalDraft,
    acknowledgeConflict,
    serverVersion,
    baseVersion,
    briefRevisions,
    loadingBriefRevisions,
    failedBriefRevisions,
    refreshBriefRevisions,
    acceptedBrief,
    loadingAcceptedBrief,
    saveDraft,
    saveBriefRevision,
    adoptBrief,
    mutations,
  } = workspace;

  const statusRef = useRef<HTMLDivElement>(null);
  const { registerCompletion } = mutations;
  const isBriefAction = !mutations.actionKind || ['brief-draft', 'brief-revision', 'brief-adoption'].includes(mutations.actionKind);
  useEffect(() => { if (saveSuccess) statusRef.current?.focus(); }, [saveSuccess]);
  useEffect(() => {
    const unregister = [
      registerCompletion('brief-draft', () => { setSaveSuccess('Draft saved successfully.'); setLocalError(null); }),
      registerCompletion('brief-revision', value => { setSaveSuccess('Revision #' + (value as { revision_number: number }).revision_number + ' saved.'); setLocalError(null); }),
      registerCompletion('brief-adoption', () => { setSaveSuccess('Revision adopted as accepted brief.'); setLocalError(null); }),
    ];
    return () => unregister.forEach(remove => remove());
  }, [registerCompletion]);
  const incompleteRows = [draftContent.outcomes, draftContent.scope, draftContent.exclusions,
    draftContent.decisions, draftContent.assumptions, draftContent.open_questions].some(rows => rows?.some(row => !row.trim())) ||
    draftContent.requirements?.some(requirement => !requirement.text.trim() || requirement.acceptance_criteria?.some(criterion => !criterion.trim()));
  const titleChanged = draftTitle !== epic?.title;

  if (loading && !epic) {
    return <LoadingStatus>Loading epic requirements brief…</LoadingStatus>;
  }

  if (failed && !epic) {
    return (
      <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)]">
        <p>Failed to load epic workspace.</p>
        <Button onClick={refreshEpic} className="mt-2">
          Retry
        </Button>
      </div>
    );
  }

  // Helper to add item to array in draftContent
  const addItem = (field: BriefListField) => {
    updateDraftContent(prev => ({
      ...prev,
      [field]: [...(prev[field] ?? []), ''],
    }));
  };

  const updateItem = (
    field: BriefListField,
    index: number,
    value: string
  ) => {
    updateDraftContent(prev => {
      const arr = [...(prev[field] ?? [])];
      arr[index] = value;
      return { ...prev, [field]: arr };
    });
  };

  const removeItem = (
    field: BriefListField,
    index: number
  ) => {
    updateDraftContent(prev => {
      const arr = [...(prev[field] ?? [])];
      arr.splice(index, 1);
      return { ...prev, [field]: arr };
    });
  };

  const handleSaveDraft = async (e?: FormEvent) => {
    e?.preventDefault();
    setSaveSuccess(null);
    try {
      await saveDraft();
    } catch {
      // Mutation hook captures errors
    }
  };

  const handleSaveRevision = async () => {
    setSaveSuccess(null);
    try {
      await saveBriefRevision();
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : 'Could not save the revision.');
    }
  };

  const handleAdopt = async (revId: string, digest: string) => {
    setSaveSuccess(null);
    try {
      await adoptBrief(revId, digest);
    } catch {
      // Mutation hook captures errors
    }
  };

  return (
    <div className="brief-workspace space-y-6">
      {onBrainstorm && <Panel title="Start with an idea" description="A rough idea is enough. Brainstorm with the assistant, then review any proposed brief before adopting it.">
        <Button variant="secondary" onClick={onBrainstorm}>Get help brainstorming</Button>
      </Panel>}
      {/* Navigation tabs */}
      <nav aria-label="Brief views" className="flex space-x-2 border-b border-[var(--border)] pb-2">
        <Button
          variant={viewTab === 'editor' ? 'primary' : 'quiet'}
          aria-current={viewTab === 'editor' ? 'page' : undefined}
          onClick={() => setViewTab('editor')}
        >
          Draft Editor
        </Button>
        <Button
          variant={viewTab === 'accepted' ? 'primary' : 'quiet'}
          aria-current={viewTab === 'accepted' ? 'page' : undefined}
          onClick={() => setViewTab('accepted')}
        >
          Accepted Brief
        </Button>
        <Button
          variant={viewTab === 'history' ? 'primary' : 'quiet'}
          aria-current={viewTab === 'history' ? 'page' : undefined}
          onClick={() => setViewTab('history')}
        >
          History & Revisions ({briefRevisions.length})
        </Button>
      </nav>

      {/* Uncertainty Retry Banner */}
      {mutations.hasPendingRetry && !mutations.loading && !mutations.shared && (
        <div role="alert" className="p-4 bg-[var(--warning-soft)] text-[var(--warning)] rounded border border-[var(--border)] space-y-2">
          <p className="font-semibold">Network or server error. Mutation outcome uncertain.</p>
          <p className="text-sm">
            The previous mutation request may have succeeded or failed on the server. To avoid duplicate side-effects, you can retry the original request with its frozen key and body.
          </p>
          <div className="flex space-x-2">
            <Button variant="primary" onClick={() => { void mutations.retryPending().catch(() => undefined); }}>
              Retry original request
            </Button>
          </div>
        </div>
      )}

      {/* External Server Conflict Banner */}
      {hasServerConflict && (
        <div role="alert" className="p-4 bg-[var(--warning-soft)] text-[var(--warning)] rounded border border-[var(--border)] space-y-2">
          <p className="font-semibold">
            The epic version changed on the server (Server version: {serverVersion}, Local base: {baseVersion}).
          </p>
          <p className="text-sm">
            Your local edits are preserved. Inspect the current server values before choosing which version to continue from.
          </p>
          <details><summary>Inspect current server brief</summary><p>Title: {epic?.title}</p><pre className="overflow-auto text-xs">{JSON.stringify(epic?.draft, null, 2)}</pre></details>
          <div className="flex space-x-2">
            <Button variant="secondary" onClick={acknowledgeConflict}>
              Use my edits against this version
            </Button>
            <Button variant="quiet" onClick={revertLocalDraft}>
              Load server version
            </Button>
          </div>
        </div>
      )}

      {/* Stale CAS Conflict Banner */}
      {mutations.conflict && isBriefAction && (
        <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)] space-y-2">
          <p className="font-semibold">Epic version conflict: The epic version changed on the server before saving.</p>
          <p className="text-sm">Please refresh the epic projections to review latest updates before modifying.</p>
          <Button variant="secondary" onClick={() => { mutations.clearError(); refreshProjections(); }}>
            Refresh projections
          </Button>
        </div>
      )}
      {(mutations.error && !mutations.conflict && !mutations.hasPendingRetry && isBriefAction) && <p role="alert">{mutations.error}</p>}
      {localError && <p role="alert">{localError}</p>}

      {/* Success Notification */}
      {saveSuccess && (
        <div role="status" ref={statusRef} tabIndex={-1} className="p-3 bg-[var(--success-soft)] text-[var(--success)] rounded text-sm">
          {saveSuccess}
        </div>
      )}

      {/* VIEW: DRAFT EDITOR */}
      {viewTab === 'editor' && (
        <form onSubmit={handleSaveDraft} className="space-y-6">
          <fieldset disabled={mutations.loading || mutations.hasPendingRetry} className="space-y-6 min-w-0">
          <legend className="sr-only">Requirements brief editor</legend>
          {incompleteRows && <p role="status">Complete or remove empty rows before saving.</p>}
          {titleChanged && <p role="status">Save the title as part of the draft before creating a content revision.</p>}
          <Panel
            title="Epic Title & Overview"
            className="[&>.panel-heading]:flex-col sm:[&>.panel-heading]:flex-row"
            description={`Version ${baseVersion} ${isDirty ? '• (Unsaved edits)' : '• (Clean)'}`}
            action={
              <div className="flex flex-wrap gap-2">
                <Button
                  type="submit"
                  variant="primary"
                  disabled={mutations.loading || mutations.hasPendingRetry || hasServerConflict || incompleteRows || !draftTitle.trim() || (!isDirty && !hasServerConflict)}
                >
                  {mutations.loading ? 'Saving…' : 'Save draft'}
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={mutations.loading || mutations.hasPendingRetry || hasServerConflict || incompleteRows || titleChanged}
                  onClick={handleSaveRevision}
                >
                  Save as new revision
                </Button>
              </div>
            }
          >
            <div className="space-y-4">
              <div>
                <label htmlFor="epic-title" className="block text-sm font-medium mb-1">
                  Title
                </label>
                <input
                  id="epic-title"
                  type="text"
                  className="w-full px-3 py-2 border border-[var(--control-border)] rounded bg-[var(--surface)] text-[var(--text)] focus:border-[var(--focus)] focus:outline-none"
                  value={draftTitle}
                  onChange={e => updateDraftTitle(e.target.value)}
                  maxLength={256}
                  required
                />
              </div>

              <div>
                <label htmlFor="epic-problem" className="block text-sm font-medium mb-1">
                  Problem Statement
                </label>
                <textarea
                  id="epic-problem"
                  className="w-full px-3 py-2 border border-[var(--control-border)] rounded bg-[var(--surface)] text-[var(--text)] focus:border-[var(--focus)] focus:outline-none min-h-[100px]"
                  value={draftContent.problem}
                  onChange={e => updateDraftProblem(e.target.value)}
                  maxLength={10000}
                  placeholder="Describe the core problem this epic solves..."
                />
              </div>
            </div>
          </Panel>

          {/* Requirements with Stable IDs and Ordered Criteria */}
          <Panel
            title="Requirements & Acceptance Criteria"
            className="[&>.panel-heading]:flex-col sm:[&>.panel-heading]:flex-row"
            description="Requirements have stable UUIDs that persist across text revisions. Graph items reference these stable IDs."
            action={
              <Button type="button" variant="secondary" onClick={() => addRequirement()}>
                + Add Requirement
              </Button>
            }
          >
            <div className="space-y-4">
              {(draftContent.requirements ?? []).length === 0 ? (
                <p className="text-sm text-[var(--muted)]">No requirements specified yet.</p>
              ) : (
                (draftContent.requirements ?? []).map((req: BriefRequirement, reqIdx: number) => (
                  <div
                    key={req.requirement_id}
                    className="p-4 border border-[var(--border)] rounded bg-[var(--surface)] space-y-3"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-sm">Requirement #{reqIdx + 1}</span>
                      <Button
                        type="button"
                        variant="quiet"
                        className="text-[var(--danger)] text-sm"
                        onClick={() => removeRequirement(req.requirement_id)}
                      >
                        Remove
                      </Button>
                    </div>

                    <EvidenceDetails summary="Inspect stable requirement ID">
                      <span>requirement_id: {req.requirement_id}</span>
                    </EvidenceDetails>

                    <div>
                      <label
                        htmlFor={`req-text-${req.requirement_id}`}
                        className="block text-xs font-medium text-[var(--muted)] mb-1"
                      >
                        Requirement text
                      </label>
                      <input
                        id={`req-text-${req.requirement_id}`}
                        type="text"
                        className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded bg-[var(--surface)] text-sm"
                        value={req.text}
                        onChange={e => updateRequirement(req.requirement_id, e.target.value)}
                        placeholder="Requirement statement..."
                      />
                    </div>

                    <div className="pl-4 border-l-2 border-[var(--border)] space-y-2">
                      <div className="flex items-center justify-between">
                        <span className="text-xs font-semibold text-[var(--muted)]">Acceptance Criteria</span>
                        <Button
                          type="button"
                          variant="quiet"
                          className="text-xs"
                          onClick={() => {
                            const updatedCrit = [...(req.acceptance_criteria ?? []), ''];
                            updateRequirement(req.requirement_id, req.text, updatedCrit);
                          }}
                        >
                          + Add criterion
                        </Button>
                      </div>

                      {(req.acceptance_criteria ?? []).map((crit, cIdx) => (
                        <div key={cIdx} className="flex items-center space-x-2">
                          <input
                            type="text"
                            aria-label={`Criterion ${cIdx + 1} for requirement ${reqIdx + 1}`}
                            className="flex-1 px-2.5 py-1 text-xs border border-[var(--control-border)] rounded bg-[var(--surface)]"
                            value={crit}
                            onChange={e => {
                              const updated = [...(req.acceptance_criteria ?? [])];
                              updated[cIdx] = e.target.value;
                              updateRequirement(req.requirement_id, req.text, updated);
                            }}
                            placeholder="Criterion description..."
                          />
                          <Button
                            type="button"
                            variant="quiet"
                            className="text-xs text-[var(--danger)]"
                            onClick={() => {
                              const updated = [...(req.acceptance_criteria ?? [])];
                              updated.splice(cIdx, 1);
                              updateRequirement(req.requirement_id, req.text, updated);
                            }}
                          >
                            ×
                          </Button>
                        </div>
                      ))}
                    </div>
                  </div>
                ))
              )}
            </div>
          </Panel>

          {/* Outcomes, Scope & Exclusions */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <Panel
              title="Outcomes"
              action={
                <Button type="button" variant="quiet" onClick={() => addItem('outcomes')}>
                  + Add
                </Button>
              }
            >
              <BriefListRows
                rows={draftContent.outcomes ?? []}
                field="outcomes"
                update={(idx, val) => updateItem('outcomes', idx, val)}
                remove={idx => removeItem('outcomes', idx)}
              />
            </Panel>

            <Panel
              title="Scope"
              action={
                <Button type="button" variant="quiet" onClick={() => addItem('scope')}>
                  + Add
                </Button>
              }
            >
              <BriefListRows
                rows={draftContent.scope ?? []}
                field="scope"
                update={(idx, val) => updateItem('scope', idx, val)}
                remove={idx => removeItem('scope', idx)}
              />
            </Panel>

            <Panel
              title="Exclusions"
              action={
                <Button type="button" variant="quiet" onClick={() => addItem('exclusions')}>
                  + Add
                </Button>
              }
            >
              <BriefListRows
                rows={draftContent.exclusions ?? []}
                field="exclusions"
                update={(idx, val) => updateItem('exclusions', idx, val)}
                remove={idx => removeItem('exclusions', idx)}
              />
            </Panel>
          </div>

          {/* Distinct Decision / Assumption / Open Question Panels */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div data-testid="brief-decisions" className="p-4 rounded border-2 border-[var(--accent)] bg-[var(--accent-soft)] space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold text-sm text-[var(--accent)]">Confirmed Decisions</h3>
                <Button type="button" variant="quiet" onClick={() => addItem('decisions')}>
                  + Add
                </Button>
              </div>
              <p className="text-xs text-[var(--muted)]">Recorded immutable commitments and architectural choices.</p>
              <BriefListRows
                rows={draftContent.decisions ?? []}
                field="decisions"
                inputClassName="flex-1 px-2 py-1 text-sm border border-[var(--control-border)] rounded bg-white"
                update={(idx, val) => updateItem('decisions', idx, val)}
                remove={idx => removeItem('decisions', idx)}
              />
            </div>

            <div data-testid="brief-assumptions" className="p-4 rounded border-2 border-[var(--info)] bg-[var(--info-soft)] space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold text-sm text-[var(--info)]">Assumptions</h3>
                <Button type="button" variant="quiet" onClick={() => addItem('assumptions')}>
                  + Add
                </Button>
              </div>
              <p className="text-xs text-[var(--muted)]">Presumed facts or dependencies subject to verification.</p>
              <BriefListRows
                rows={draftContent.assumptions ?? []}
                field="assumptions"
                inputClassName="flex-1 px-2 py-1 text-sm border border-[var(--control-border)] rounded bg-white"
                update={(idx, val) => updateItem('assumptions', idx, val)}
                remove={idx => removeItem('assumptions', idx)}
              />
            </div>

            <div data-testid="brief-open-questions" className="p-4 rounded border-2 border-[var(--warning)] bg-[var(--warning-soft)] space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold text-sm text-[var(--warning)]">Open Questions</h3>
                <Button type="button" variant="quiet" onClick={() => addItem('open_questions')}>
                  + Add
                </Button>
              </div>
              <p className="text-xs text-[var(--muted)]">Unresolved requirements or architectural inquiries.</p>
              <BriefListRows
                rows={draftContent.open_questions ?? []}
                field="open_questions"
                inputClassName="flex-1 px-2 py-1 text-sm border border-[var(--control-border)] rounded bg-white"
                update={(idx, val) => updateItem('open_questions', idx, val)}
                remove={idx => removeItem('open_questions', idx)}
              />
            </div>
          </div>
          </fieldset>
        </form>
      )}

      {/* VIEW: ACCEPTED BRIEF */}
      {viewTab === 'accepted' && (
        <Panel
          title="Accepted Brief Baseline"
          description="The chosen requirements baseline. Choosing a different brief requires a new matching work-item graph."
          action={
            epic?.accepted_brief_revision_id ? (
              <StatusBadge label="Adopted Baseline" tone="success" />
            ) : (
              <StatusBadge label="No Brief Adopted" tone="warning" />
            )
          }
        >
          {!acceptedBrief ? (
            <div className="p-4 bg-[var(--surface-muted)] rounded text-sm text-[var(--muted)] space-y-2">
              <p>{epic?.accepted_brief_revision_id ? loadingAcceptedBrief ? 'Loading the accepted brief…' : 'The accepted brief could not be loaded. Refresh the workspace to retry.' : 'No brief has been accepted yet for this epic.'}</p>
              {epic?.accepted_brief_revision_id ? !loadingAcceptedBrief && (
                <Button variant="secondary" onClick={refreshProjections}>Retry accepted brief</Button>
              ) : <p>
                Save a revision and click &quot;Adopt revision&quot; in the History tab to establish an authoritative baseline.
              </p>}
            </div>
          ) : (
            <div className="space-y-4">
              <div className="p-3 bg-[var(--surface-muted)] rounded space-y-1 text-sm">
                <p>Accepted requirements baseline</p>
                <EvidenceDetails summary="Inspect revision and digest">
                  <p>brief_revision_id: {acceptedBrief.brief_revision_id}</p>
                  <span>brief_digest: {acceptedBrief.brief_digest}</span>
                </EvidenceDetails>
              </div>

              <div>
                <h4 className="font-semibold text-sm mb-1">Problem Statement</h4>
                <p className="text-sm bg-[var(--surface-muted)] p-3 rounded">{acceptedBrief.problem}</p>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                {([
                  ['Outcomes', acceptedBrief.outcomes ?? []],
                  ['Scope', acceptedBrief.scope ?? []],
                  ['Exclusions', acceptedBrief.exclusions ?? []],
                ] as const).map(([title, rows]) => <div key={title} className="p-3 border border-[var(--border)] rounded text-sm">
                  <h4 className="font-semibold mb-1">{title}</h4>
                  {rows.length ? <ul className="list-disc pl-4 space-y-1">{rows.map((row, index) => <li key={index}>{row}</li>)}</ul> : <p className="text-[var(--muted)]">None recorded.</p>}
                </div>)}
              </div>

              <div>
                <h4 className="font-semibold text-sm mb-2">Requirements</h4>
                <div className="space-y-2">
                  {(acceptedBrief.requirements ?? []).map((req, idx) => (
                    <div key={req.requirement_id} className="p-3 border border-[var(--border)] rounded text-sm space-y-1">
                      <p className="font-medium">
                        {idx + 1}. {req.text}
                      </p>
                      <EvidenceDetails summary="Requirement ID">
                        <span>{req.requirement_id}</span>
                      </EvidenceDetails>
                      <ul className="list-disc pl-5 text-xs text-[var(--muted)] space-y-0.5 mt-1">
                        {(req.acceptance_criteria ?? []).map((crit, cIdx) => (
                          <li key={cIdx}>{crit}</li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <div className="p-3 rounded border border-[var(--accent)] bg-[var(--accent-soft)] text-sm">
                  <h5 className="font-semibold text-[var(--accent)] mb-1">Decisions</h5>
                  <ul className="list-disc pl-4 text-xs space-y-1">
                    {(acceptedBrief.decisions ?? []).map((d, i) => (
                      <li key={i}>{d}</li>
                    ))}
                  </ul>
                </div>
                <div className="p-3 rounded border border-[var(--info)] bg-[var(--info-soft)] text-sm">
                  <h5 className="font-semibold text-[var(--info)] mb-1">Assumptions</h5>
                  <ul className="list-disc pl-4 text-xs space-y-1">
                    {(acceptedBrief.assumptions ?? []).map((a, i) => (
                      <li key={i}>{a}</li>
                    ))}
                  </ul>
                </div>
                <div className="p-3 rounded border border-[var(--warning)] bg-[var(--warning-soft)] text-sm">
                  <h5 className="font-semibold text-[var(--warning)] mb-1">Open Questions</h5>
                  <ul className="list-disc pl-4 text-xs space-y-1">
                    {(acceptedBrief.open_questions ?? []).map((q, i) => (
                      <li key={i}>{q}</li>
                    ))}
                  </ul>
                </div>
              </div>
            </div>
          )}
        </Panel>
      )}

      {/* VIEW: HISTORY & REVISIONS */}
      {viewTab === 'history' && (
        <Panel
          title="Brief Revision History"
          description="Immutable snapshots saved on the server. Adopting a different revision clears accepted graph eligibility."
        >
          {loadingBriefRevisions && briefRevisions.length === 0 && <LoadingStatus>Loading brief history…</LoadingStatus>}
          {failedBriefRevisions && <div role="alert" className="space-y-2">
            <p>Brief history could not be loaded.</p>
            <Button variant="secondary" onClick={refreshBriefRevisions}>Retry brief history</Button>
          </div>}
          {!loadingBriefRevisions && !failedBriefRevisions && briefRevisions.length === 0 && (
            <p className="text-sm text-[var(--muted)]">No revisions have been saved yet.</p>
          )}
          {briefRevisions.length > 0 && (
            <div className="space-y-3">
              {briefRevisions.map(rev => {
                const isAccepted = rev.brief_revision_id === epic?.accepted_brief_revision_id;
                return (
                  <div
                    key={rev.brief_revision_id}
                    className="p-4 border border-[var(--border)] rounded bg-[var(--surface)] flex flex-col md:flex-row md:items-center justify-between gap-4"
                  >
                    <div className="space-y-1">
                      <div className="flex items-center space-x-2">
                        <span className="font-semibold text-sm">Revision #{rev.revision_number}</span>
                        {isAccepted && <StatusBadge label="Currently Accepted" tone="success" />}
                        <span className="text-xs text-[var(--muted)]">
                          {new Date(rev.created_at).toLocaleString()}
                        </span>
                      </div>
                      <p className="text-sm text-[var(--muted)] line-clamp-1">{rev.content?.problem}</p>
                      <EvidenceDetails summary="Inspect revision IDs and digest">
                        <p>brief_revision_id: {rev.brief_revision_id}</p>
                        <p>content_digest: {rev.content_digest}</p>
                        <pre className="overflow-auto">{JSON.stringify(rev.content, null, 2)}</pre>
                      </EvidenceDetails>
                    </div>

                    <div>
                      {!isAccepted && (
                        <Button
                          variant="secondary"
                          disabled={mutations.loading || mutations.hasPendingRetry}
                          onClick={() => handleAdopt(rev.brief_revision_id, rev.content_digest)}
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
