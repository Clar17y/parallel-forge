'use client';

import { useState, type FormEvent } from 'react';
import { Button } from '@/components/ui/button';
import { Panel } from '@/components/ui/panel';
import { StatusBadge } from '@/components/ui/status-badge';
import { useEpicBudget } from '@/hooks/epics/use-epic-budget';
import { EvidenceDetails } from './evidence-details';
import type { TaskBudget } from '@/hooks/epics/types';

interface DimensionConfig {
  key: string;
  label: string;
  formatUsage: (val: number, currency?: string | null) => string;
  formatCeiling: (val: number, currency?: string | null) => string;
  getCeilingVal: (ceiling: TaskBudget) => number | null | undefined;
  setCeilingVal: (ceiling: TaskBudget, val: number | null) => TaskBudget;
  inputLabel: string;
  unlimitedLabel: string;
}

const DIMENSIONS: DimensionConfig[] = [
  {
    key: 'duration_ms',
    label: 'Duration',
    formatUsage: val => `${Math.round(val / 1000)}s (${val}ms)`,
    formatCeiling: val => `${val}s`,
    getCeilingVal: c => c?.max_duration_seconds,
    setCeilingVal: (c, val) => ({ ...(c ?? {}), max_duration_seconds: val ?? 0 } as TaskBudget),
    inputLabel: 'Duration ceiling (seconds)',
    unlimitedLabel: 'Unlimited duration',
  },
  {
    key: 'tool_call_count',
    label: 'Tool Calls',
    formatUsage: val => `${val}`,
    formatCeiling: val => `${val}`,
    getCeilingVal: c => c?.max_tool_calls,
    setCeilingVal: (c, val) => ({ ...(c ?? {}), max_tool_calls: val ?? 0 } as TaskBudget),
    inputLabel: 'Tool calls ceiling',
    unlimitedLabel: 'Unlimited tool calls',
  },
  {
    key: 'input_tokens',
    label: 'Input Tokens',
    formatUsage: val => `${val.toLocaleString()} tokens`,
    formatCeiling: val => `${val.toLocaleString()} tokens`,
    getCeilingVal: c => c?.max_input_tokens,
    setCeilingVal: (c, val) => ({ ...(c ?? {}), max_input_tokens: val } as TaskBudget),
    inputLabel: 'Input tokens ceiling',
    unlimitedLabel: 'Unlimited input tokens',
  },
  {
    key: 'output_tokens',
    label: 'Output Tokens',
    formatUsage: val => `${val.toLocaleString()} tokens`,
    formatCeiling: val => `${val.toLocaleString()} tokens`,
    getCeilingVal: c => c?.max_output_tokens,
    setCeilingVal: (c, val) => ({ ...(c ?? {}), max_output_tokens: val } as TaskBudget),
    inputLabel: 'Output tokens ceiling',
    unlimitedLabel: 'Unlimited output tokens',
  },
  {
    key: 'estimated_api_cost_minor',
    label: 'Estimated Cost',
    formatUsage: (val, curr) => `${val} minor units${curr ? ` (${curr})` : ''}`,
    formatCeiling: (val, curr) => `${val} minor units${curr ? ` (${curr})` : ''}`,
    getCeilingVal: c => c?.max_cost_minor,
    setCeilingVal: (c, val) => ({ ...(c ?? {}), max_cost_minor: val } as TaskBudget),
    inputLabel: 'Cost ceiling (minor units)',
    unlimitedLabel: 'Unlimited cost',
  },
  {
    key: 'provider_attempts',
    label: 'Provider Attempts',
    formatUsage: val => `${val}`,
    formatCeiling: val => `${val}`,
    getCeilingVal: c => c?.max_provider_attempts,
    setCeilingVal: (c, val) => ({ ...(c ?? {}), max_provider_attempts: val ?? 0 } as TaskBudget),
    inputLabel: 'Provider attempts ceiling',
    unlimitedLabel: 'Unlimited provider attempts',
  },
];

export function EpicBudgetPanel({
  epicId,
  title = 'Shared Epic Budget & Ceilings',
  description = 'Exposes shared ceilings, actual aggregate counter usage, held capacity, and unknown exposure across epochs and roles.',
}: {
  epicId: string;
  title?: string;
  description?: string;
}) {
  const {
    budget,
    loading,
    failed,
    refresh,
    readLatestBudget,
    isUnlimited,
    getKnown,
    getHeld,
    isUnknown,
    currency,
    warnings,
    permits,
    ownerActions,
    editBudget,
    permitBudgetAdmission,
    mutations,
  } = useEpicBudget(epicId);

  // Edit ceilings form state
  const [isEditing, setIsEditing] = useState(false);
  const [sourceVersion, setSourceVersion] = useState<number | null>(null);
  const [sourceCeiling, setSourceCeiling] = useState<TaskBudget | null>(null);
  const [editCeilings, setEditCeilings] = useState<Record<string, number | null>>({});
  const [disabledDims, setDisabledDims] = useState<string[]>([]);
  const [touchedCeilings, setTouchedCeilings] = useState<Set<string>>(new Set());
  const [touchedDisabledDims, setTouchedDisabledDims] = useState<Set<string>>(new Set());
  const [editNote, setEditNote] = useState('');
  const [isRebasing, setIsRebasing] = useState(false);
  const [rebaseError, setRebaseError] = useState<string | null>(null);

  // Permit form state
  const [isRequestingPermit, setIsRequestingPermit] = useState(false);
  const [permitRunId, setPermitRunId] = useState('');
  const [permitNote, setPermitNote] = useState('');

  const initEditForm = () => {
    if (!budget) return;
    const initialCeilings: Record<string, number | null> = {};
    for (const dim of DIMENSIONS) {
      const val = dim.getCeilingVal(budget.ceiling);
      initialCeilings[dim.key] = val ?? null;
    }
    setSourceVersion(budget.version);
    setSourceCeiling({ ...budget.ceiling });
    setEditCeilings(initialCeilings);
    setDisabledDims([...budget.disabled_dimensions]);
    setTouchedCeilings(new Set());
    setTouchedDisabledDims(new Set());
    setEditNote('');
    setRebaseError(null);
    setIsEditing(true);
  };

  const handleToggleUnlimited = (dimKey: string) => {
    setDisabledDims(prev =>
      prev.includes(dimKey) ? prev.filter(k => k !== dimKey) : [...prev, dimKey]
    );
    setTouchedDisabledDims(prev => new Set(prev).add(dimKey));
  };

  const handleCeilingChange = (dimKey: string, value: number | null) => {
    setEditCeilings(prev => ({ ...prev, [dimKey]: value }));
    setTouchedCeilings(prev => new Set(prev).add(dimKey));
  };

  const handleRebase = async () => {
    if (!budget || isRebasing || mutations.loading || mutations.hasPendingRetry) return;
    setIsRebasing(true);
    setRebaseError(null);
    try {
      const latest = await readLatestBudget();
      setSourceVersion(latest.version);
      setSourceCeiling({ ...latest.ceiling });
      setEditCeilings(prev => {
        const next = { ...prev };
        for (const dim of DIMENSIONS) {
          if (!touchedCeilings.has(dim.key)) {
            next[dim.key] = dim.getCeilingVal(latest.ceiling) ?? null;
          }
        }
        return next;
      });
      setDisabledDims(prev => {
        const preservedTouched = prev.filter(k => touchedDisabledDims.has(k));
        const latestUntouched = latest.disabled_dimensions.filter(k => !touchedDisabledDims.has(k));
        return [...new Set([...preservedTouched, ...latestUntouched])];
      });
      mutations.clearError();
      refresh();
    } catch {
      setRebaseError('Could not load the latest budget. Your changes are preserved; try rebasing again.');
    } finally {
      setIsRebasing(false);
    }
  };

  const handleSaveCeilings = async (e: FormEvent) => {
    e.preventDefault();
    if (mutationPending || !budget || !sourceCeiling || sourceVersion === null) return;

    let updatedCeiling: TaskBudget = { ...sourceCeiling };
    for (const dim of DIMENSIONS) {
      if (touchedCeilings.has(dim.key)) {
        const val = editCeilings[dim.key];
        updatedCeiling = dim.setCeilingVal(updatedCeiling, val);
      }
    }

    try {
      await editBudget({
        ceiling: updatedCeiling,
        expectedVersion: sourceVersion,
        disabledDimensions: disabledDims,
        note: editNote.trim() || undefined,
      });
      setIsEditing(false);
      setTouchedCeilings(new Set());
      setTouchedDisabledDims(new Set());
    } catch {
      // Mutations store captures error; keep user edits
    }
  };

  const handleIssuePermit = async (e: FormEvent) => {
    e.preventDefault();
    if (mutationPending || !budget || !permitRunId.trim()) return;

    try {
      await permitBudgetAdmission({
        runId: permitRunId.trim(),
        expectedVersion: budget.version,
        note: permitNote.trim() || undefined,
      });
      setIsRequestingPermit(false);
      setPermitRunId('');
      setPermitNote('');
    } catch {
      // Mutations store captures error
    }
  };

  const mutationPending = mutations.loading || mutations.hasPendingRetry || isRebasing;

  if (loading && !budget) {
    return <p role="status">Loading budget projection…</p>;
  }

  if (failed && !budget) {
    return (
      <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)] space-y-2">
        <p>Failed to load epic budget.</p>
        <Button variant="secondary" onClick={refresh}>Retry</Button>
      </div>
    );
  }

  if (!budget || !budget.ceiling) return null;

  return (
    <Panel
      title={title}
      description={`${description} • Version ${budget.version}${currency ? ` • Currency: ${currency}` : ''}`}
    >
      <div className="space-y-4">
        {/* Uncertainty Banner */}
        {mutations.hasPendingRetry && !mutations.shared && (
          <div role="alert" className="p-4 bg-[var(--warning-soft)] text-[var(--warning)] rounded border border-[var(--border)] space-y-2">
            <p className="font-semibold">Network or server error. Mutation outcome uncertain.</p>
            <Button
              variant="primary"
              disabled={mutations.loading}
              onClick={() => { void mutations.retryPending().catch(() => undefined); }}
            >
              {mutations.loading ? 'Retrying…' : 'Retry original request'}
            </Button>
          </div>
        )}

        {/* Conflict Alert */}
        {mutations.conflict && (
          <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)] space-y-2">
            <p className="font-semibold">{mutations.error}</p>
            <div className="flex flex-wrap gap-2">
              {isEditing && (
                <Button variant="primary" disabled={mutationPending} onClick={handleRebase}>
                  {isRebasing ? 'Loading latest budget…' : 'Rebase changes onto latest version'}
                </Button>
              )}
              <Button variant="secondary" disabled={isRebasing} onClick={() => { mutations.clearError(); refresh(); }}>
                Refresh budget
              </Button>
            </div>
          </div>
        )}

        {rebaseError && <p role="alert">{rebaseError}</p>}

        {/* Generic Error */}
        {mutations.error && !mutations.conflict && (
          <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)] space-y-2">
            <p>{mutations.error}</p>
            <Button variant="quiet" onClick={mutations.clearError}>Dismiss message</Button>
          </div>
        )}

        {/* Warnings */}
        {warnings.length > 0 && (
          <div role="alert" className="p-3 bg-[var(--warning-soft)] text-[var(--warning)] rounded border border-[var(--warning)] space-y-1 text-sm">
            <p className="font-semibold">Budget Warnings</p>
            <ul className="list-disc pl-5 space-y-0.5">
              {warnings.map((w, idx) => <li key={idx}>{w}</li>)}
            </ul>
          </div>
        )}

        {/* 6 Dimensions Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {DIMENSIONS.map(dim => {
            const unlimited = isUnlimited(dim.key);
            const knownVal = getKnown(dim.key);
            const heldVal = getHeld(dim.key);
            const rawCeiling = dim.getCeilingVal(budget.ceiling);

            return (
              <div key={dim.key} className="p-3 bg-[var(--surface-muted)] border border-[var(--border)] rounded space-y-1 text-sm">
                <div className="flex items-center justify-between">
                  <span className="font-semibold">{dim.label}</span>
                  {unlimited ? (
                    <StatusBadge label="Disabled (shared unlimited)" tone="info" />
                  ) : rawCeiling == null ? (
                    <StatusBadge label="Unlimited (null)" tone="info" />
                  ) : null}
                </div>

                <div className="text-xs text-[var(--muted)] space-y-0.5">
                  <p data-testid={`ceiling-${dim.key}`}>
                    Ceiling:{' '}
                    <span className="font-medium text-[var(--foreground)]">
                      {unlimited
                        ? 'Disabled shared cap (unlimited)'
                        : rawCeiling == null
                        ? 'Unlimited (null)'
                        : rawCeiling === 0
                        ? dim.formatCeiling(0, currency)
                        : dim.formatCeiling(rawCeiling, currency)}
                    </span>
                  </p>
                  <p data-testid={`known-${dim.key}`}>
                    Known usage:{' '}
                    <span className="font-medium text-[var(--foreground)]">
                      {dim.formatUsage(knownVal, currency)}
                    </span>
                  </p>
                  <p data-testid={`held-${dim.key}`}>
                    Held capacity:{' '}
                    <span className="font-medium text-[var(--foreground)]">
                      {dim.formatUsage(heldVal, currency)}
                    </span>
                  </p>
                </div>
              </div>
            );
          })}
        </div>

        {/* Unknown exposure info */}
        <p className="text-xs text-[var(--muted)]">
          {isUnknown
            ? 'Some usage is unknown. Unknown usage is not zero.'
            : 'All reported resource usage is currently accounted for.'}
        </p>

        {/* Action Buttons */}
        <div className="flex flex-wrap gap-2 pt-2 border-t border-[var(--border)]">
          {!isEditing && (
            <Button
              variant="secondary"
              disabled={mutationPending}
              onClick={initEditForm}
            >
              Edit Budget Ceilings
            </Button>
          )}
          {!isRequestingPermit && (
            <Button
              variant="quiet"
              disabled={mutationPending}
              onClick={() => setIsRequestingPermit(true)}
            >
              Request Admission Permit
            </Button>
          )}
          <Button variant="quiet" onClick={refresh}>
            Refresh Budget
          </Button>
        </div>

        {/* Edit Ceilings Form */}
        {isEditing && (
          <form onSubmit={handleSaveCeilings} className="p-4 border border-[var(--border)] rounded bg-[var(--surface)] space-y-4 text-sm mt-3">
            <h3 className="font-semibold text-base">Edit Shared Ceilings</h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {DIMENSIONS.map(dim => {
                const unlimited = disabledDims.includes(dim.key);
                return (
                  <div key={dim.key} className="space-y-1">
                    <label htmlFor={`ceiling-input-${dim.key}`} className="block font-medium text-xs">
                      {dim.inputLabel}
                    </label>
                    <input
                      id={`ceiling-input-${dim.key}`}
                      type="number"
                      min={0}
                      className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)] disabled:opacity-50"
                      value={editCeilings[dim.key] ?? ''}
                      placeholder={unlimited ? 'Unlimited' : editCeilings[dim.key] == null ? 'Unlimited (null)' : '0'}
                      disabled={unlimited || mutationPending}
                      onChange={e => handleCeilingChange(dim.key, e.target.value === '' ? null : Number(e.target.value))}
                    />
                    <label className="flex items-center gap-2 text-xs text-[var(--muted)] cursor-pointer pt-1">
                      <input
                        type="checkbox"
                        checked={unlimited}
                        disabled={mutationPending}
                        onChange={() => handleToggleUnlimited(dim.key)}
                      />
                      <span>{dim.unlimitedLabel}</span>
                    </label>
                  </div>
                );
              })}
            </div>

            <div className="space-y-1">
              <label htmlFor="edit-note" className="block font-medium text-xs">
                Optional owner note
              </label>
              <textarea
                id="edit-note"
                rows={2}
                className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
                placeholder="Reason for limit change..."
                value={editNote}
                disabled={mutationPending}
                onChange={e => setEditNote(e.target.value)}
              />
            </div>

            <div className="flex gap-2">
              <Button type="submit" variant="primary" disabled={mutationPending}>
                Save Ceilings
              </Button>
              <Button
                type="button"
                variant="quiet"
                disabled={mutationPending}
                onClick={() => setIsEditing(false)}
              >
                Cancel
              </Button>
            </div>
          </form>
        )}

        {/* Admission Permit Form */}
        {isRequestingPermit && (
          <form onSubmit={handleIssuePermit} className="p-4 border border-[var(--border)] rounded bg-[var(--surface)] space-y-4 text-sm mt-3">
            <h3 className="font-semibold text-base">Request Admission Permit</h3>
            <p className="text-xs text-[var(--muted)]">
              Authorizes an admission permit for an internal run exceeding normal limits. Warnings are preserved honestly.
            </p>
            <div className="space-y-1">
              <label htmlFor="permit-run-id" className="block font-medium text-xs">
                Child Run ID (UUID)
              </label>
              <input
                id="permit-run-id"
                type="text"
                required
                className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
                placeholder="00000000-0000-0000-0000-000000000000"
                value={permitRunId}
                disabled={mutationPending}
                onChange={e => setPermitRunId(e.target.value)}
              />
            </div>

            <div className="space-y-1">
              <label htmlFor="permit-note" className="block font-medium text-xs">
                Permit note (optional)
              </label>
              <textarea
                id="permit-note"
                rows={2}
                className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
                placeholder="Reason for issuing admission permit..."
                value={permitNote}
                disabled={mutationPending}
                onChange={e => setPermitNote(e.target.value)}
              />
            </div>

            <div className="flex gap-2">
              <Button type="submit" variant="primary" disabled={mutationPending || !permitRunId.trim()}>
                Issue Permit
              </Button>
              <Button
                type="button"
                variant="quiet"
                disabled={mutationPending}
                onClick={() => setIsRequestingPermit(false)}
              >
                Cancel
              </Button>
            </div>
          </form>
        )}

        {/* Evidence details for permits and owner actions */}
        {(permits.length > 0 || ownerActions.length > 0) && (
          <div className="pt-2 border-t border-[var(--border)] space-y-2">
            {permits.length > 0 && (
              <EvidenceDetails summary={`Inspect active admission permits (${permits.length})`}>
                {permits.map(p => (
                  <div key={p.permit_id} className="text-xs py-1">
                    <span>permit_id: {p.permit_id} | run_id: {p.run_id}</span>
                    {p.note && <span> | note: {p.note}</span>}
                    {p.consumed_attempt_id && <span> | consumed_by: {p.consumed_attempt_id}</span>}
                  </div>
                ))}
              </EvidenceDetails>
            )}
            {ownerActions.length > 0 && (
              <EvidenceDetails summary={`Inspect budget owner audit log (${ownerActions.length})`}>
                {ownerActions.map((oa, idx) => (
                  <div key={idx} className="text-xs py-1">
                    <span>event: {oa.event_type} | actor: {oa.actor_id}</span>
                    {oa.note && <span> | note: {oa.note}</span>}
                  </div>
                ))}
              </EvidenceDetails>
            )}
          </div>
        )}
      </div>
    </Panel>
  );
}
