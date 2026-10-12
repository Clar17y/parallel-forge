'use client';

import { useRef, useState, type FormEvent } from 'react';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/lib/api/client';
import {
  validateSafeInteger,
  type MonitoringMode,
  type MonitoringPolicyResponse,
  type MonitoringPolicyRevisionRequest,
} from '@/lib/api/monitoring-policy';
import styles from './project-policy.module.css';

export interface MonitoringPolicyEditorProps {
  projectId: string;
  initial?: MonitoringPolicyResponse | null;
  onSave: (request: MonitoringPolicyRevisionRequest) => Promise<MonitoringPolicyResponse>;
  onRefresh?: () => Promise<MonitoringPolicyResponse | null>;
  onCancel?: () => void;
}

interface PolicyDraft {
  mode: MonitoringMode;
  warningMultiplier: string;
  checkpointMultiplier: string;
  historyLimit: string;
  minimumComparables: string;
  windowDays: string;
  allowCrossProjectFallback: boolean;
  hardTokenCap: string;
  hardContextCap: string;
  manualEstimateTokens: string;
}

function draftFromPolicy(policy?: MonitoringPolicyResponse | null): PolicyDraft {
  return {
    mode: (policy?.mode as MonitoringMode) ?? 'checkpoint',
    warningMultiplier: String(policy?.warning_multiplier ?? 1.5),
    checkpointMultiplier: String(policy?.checkpoint_multiplier ?? 2),
    historyLimit: String(policy?.history_limit ?? 50),
    minimumComparables: String(policy?.minimum_comparables ?? 20),
    windowDays: policy?.window_days == null ? '' : String(policy.window_days),
    allowCrossProjectFallback: policy?.allow_cross_project_fallback ?? true,
    hardTokenCap: policy?.hard_token_cap == null ? '' : String(policy.hard_token_cap),
    hardContextCap: policy?.hard_context_cap == null ? '' : String(policy.hard_context_cap),
    manualEstimateTokens: policy?.manual_estimate_tokens == null ? '' : String(policy.manual_estimate_tokens),
  };
}

export function MonitoringPolicyEditor({
  projectId: _projectId,
  initial,
  onSave,
  onRefresh,
  onCancel,
}: MonitoringPolicyEditorProps) {
  const [expectedRevision, setExpectedRevision] = useState<number>(initial?.revision ?? 0);
  const [mode, setMode] = useState<MonitoringMode>(
    (initial?.mode as MonitoringMode) ?? 'checkpoint'
  );
  const [warningMultiplier, setWarningMultiplier] = useState<string>(
    String(initial?.warning_multiplier ?? 1.5)
  );
  const [checkpointMultiplier, setCheckpointMultiplier] = useState<string>(
    String(initial?.checkpoint_multiplier ?? 2.0)
  );
  const [historyLimit, setHistoryLimit] = useState<string>(
    String(initial?.history_limit ?? 50)
  );
  const [minimumComparables, setMinimumComparables] = useState<string>(
    String(initial?.minimum_comparables ?? 20)
  );
  const [windowDays, setWindowDays] = useState<string>(
    initial?.window_days != null ? String(initial.window_days) : ''
  );
  const [allowCrossProjectFallback, setAllowCrossProjectFallback] = useState<boolean>(
    initial?.allow_cross_project_fallback ?? true
  );

  // Advanced caps & estimate
  const [hardTokenCap, setHardTokenCap] = useState<string>(
    initial?.hard_token_cap != null ? String(initial.hard_token_cap) : ''
  );
  const [hardContextCap, setHardContextCap] = useState<string>(
    initial?.hard_context_cap != null ? String(initial.hard_context_cap) : ''
  );
  const [manualEstimateTokens, setManualEstimateTokens] = useState<string>(
    initial?.manual_estimate_tokens != null ? String(initial.manual_estimate_tokens) : ''
  );

  const [note, setNote] = useState<string>('');
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<boolean>(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const savingNow = useRef(false);
  const [refreshing, setRefreshing] = useState(false);
  const refreshingNow = useRef(false);
  const baseline = useRef<PolicyDraft>(draftFromPolicy(initial));

  function rebaseDraft(previous: PolicyDraft, latest: PolicyDraft) {
    setMode(current => current === previous.mode ? latest.mode : current);
    setWarningMultiplier(current => current === previous.warningMultiplier ? latest.warningMultiplier : current);
    setCheckpointMultiplier(current => current === previous.checkpointMultiplier ? latest.checkpointMultiplier : current);
    setHistoryLimit(current => current === previous.historyLimit ? latest.historyLimit : current);
    setMinimumComparables(current => current === previous.minimumComparables ? latest.minimumComparables : current);
    setWindowDays(current => current === previous.windowDays ? latest.windowDays : current);
    setAllowCrossProjectFallback(current => current === previous.allowCrossProjectFallback ? latest.allowCrossProjectFallback : current);
    setHardTokenCap(current => current === previous.hardTokenCap ? latest.hardTokenCap : current);
    setHardContextCap(current => current === previous.hardContextCap ? latest.hardContextCap : current);
    setManualEstimateTokens(current => current === previous.manualEstimateTokens ? latest.manualEstimateTokens : current);
    baseline.current = latest;
  }

  async function refreshAfterConflict() {
    if (!onRefresh || refreshingNow.current) return;
    refreshingNow.current = true;
    setRefreshing(true);
    try {
      const latest = await onRefresh();
      rebaseDraft(baseline.current, draftFromPolicy(latest));
      setExpectedRevision(latest?.revision ?? 0);
      setConflict(false);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not load the current policy. Your edits are preserved.');
    } finally {
      refreshingNow.current = false;
      setRefreshing(false);
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (savingNow.current || conflict || refreshingNow.current) return;

    setError(null);
    setFieldErrors({});
    const errors: Record<string, string> = {};

    // Validate multipliers
    const warnNum = Number(warningMultiplier);
    const checkNum = Number(checkpointMultiplier);
    if (!Number.isFinite(warnNum) || warnNum < 1.0) {
      errors.warning_multiplier = 'Warning multiplier must be at least 1.0.';
    }
    if (!Number.isFinite(checkNum) || checkNum < 1.0) {
      errors.checkpoint_multiplier = 'Checkpoint multiplier must be at least 1.0.';
    } else if (Number.isFinite(warnNum) && checkNum < warnNum) {
      errors.checkpoint_multiplier = 'Checkpoint multiplier must be greater than or equal to warning multiplier.';
    }

    // Validate history & minimum comparables
    const histVal = validateSafeInteger(historyLimit, {
      fieldName: 'History limit',
      allowNull: false,
      allowSigned64: true,
      min: 1,
    });
    if (!histVal.valid) {
      errors.history_limit = histVal.error!;
    }

    const minCompVal = validateSafeInteger(minimumComparables, {
      fieldName: 'Minimum comparables',
      allowNull: false,
      allowSigned64: true,
      min: 1,
    });
    if (!minCompVal.valid) {
      errors.minimum_comparables = minCompVal.error!;
    } else if (histVal.valid && BigInt(minCompVal.value!) > BigInt(histVal.value!)) {
      errors.minimum_comparables = 'Minimum comparables cannot exceed history limit.';
    }

    // Validate window days
    const windowVal = validateSafeInteger(windowDays, {
      fieldName: 'Window days',
      allowNull: true,
      allowSigned64: true,
      min: 1,
    });
    if (!windowVal.valid) {
      errors.window_days = windowVal.error!;
    }

    // Validate hard caps and estimates
    const tokenCapVal = validateSafeInteger(hardTokenCap, {
      fieldName: 'Hard token cap',
      allowNull: true,
      allowZero: false,
      allowSigned64: true,
      min: 1,
    });
    if (!tokenCapVal.valid) {
      errors.hard_token_cap = tokenCapVal.error!;
    }

    const contextCapVal = validateSafeInteger(hardContextCap, {
      fieldName: 'Hard context cap',
      allowNull: true,
      allowZero: false,
      allowSigned64: true,
      min: 1,
    });
    if (!contextCapVal.valid) {
      errors.hard_context_cap = contextCapVal.error!;
    }

    const manualEstVal = validateSafeInteger(manualEstimateTokens, {
      fieldName: 'Manual estimate tokens',
      allowNull: true,
      allowZero: false,
      allowSigned64: true,
      min: 1,
    });
    if (!manualEstVal.valid) {
      errors.manual_estimate_tokens = manualEstVal.error!;
    }

    if (Object.keys(errors).length > 0) {
      setFieldErrors(errors);
      setError(Object.values(errors)[0]);
      return;
    }

    const payload: MonitoringPolicyRevisionRequest = {
      expected_revision: expectedRevision,
      mode,
      warning_multiplier: warnNum,
      checkpoint_multiplier: checkNum,
      history_limit: histVal.value!,
      minimum_comparables: minCompVal.value!,
      window_days: windowVal.value ?? null,
      hard_token_cap: tokenCapVal.value ?? null,
      hard_context_cap: contextCapVal.value ?? null,
      manual_estimate_tokens: manualEstVal.value ?? null,
      allow_cross_project_fallback: allowCrossProjectFallback,
      note: note.trim() || null,
    };
    const submittedDraft: PolicyDraft = {
      mode, warningMultiplier, checkpointMultiplier, historyLimit, minimumComparables,
      windowDays, allowCrossProjectFallback, hardTokenCap, hardContextCap, manualEstimateTokens,
    };

    savingNow.current = true;
    setSaving(true);
    try {
      const saved = await onSave(payload);
      rebaseDraft(submittedDraft, draftFromPolicy(saved));
      setExpectedRevision(saved.revision);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        setConflict(true);
        setError(
          `Monitoring policy revision conflict: Another change was saved since loading this revision (expected revision ${expectedRevision}). Your edits are preserved below.`
        );
      } else {
        setError(cause instanceof Error ? cause.message : 'Could not save monitoring policy revision.');
      }
    } finally {
      savingNow.current = false;
      setSaving(false);
    }
  }

  return (
    <form
      noValidate
      onSubmit={handleSubmit}
      className={styles.setupSection}
      aria-label="Revise task usage monitoring policy"
    >
      <div className={styles.setupSectionHeader}>
        <h2 className={styles.setupSectionTitle}>
          Revise task usage monitoring policy · Revision {expectedRevision}
        </h2>
      </div>

      <p className={styles.sectionGuidance}>
        Configure versioned observation mode, multiplier thresholds, and baseline cohort learning. Hard caps are optional advanced ceilings; omission leaves tokens uncapped.
      </p>

      {conflict && (
        <div
          role="alert"
          className="p-3 mb-3 bg-[var(--danger-soft)] text-[var(--danger)] border border-[var(--border)] rounded text-sm space-y-2"
        >
          <p className="font-semibold">{error}</p>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="primary"
              disabled={refreshing || !onRefresh}
              onClick={refreshAfterConflict}
            >
              {refreshing ? 'Loading current revision…' : 'Load current revision and keep edits'}
            </Button>
          </div>
        </div>
      )}

      {error && !conflict && Object.keys(fieldErrors).length === 0 && (
        <p role="alert" className="p-3 mb-3 bg-[var(--danger-soft)] text-[var(--danger)] rounded text-sm font-medium">
          {error}
        </p>
      )}

      <fieldset disabled={saving} className="space-y-4">
        {/* Monitoring Mode */}
        <div className="space-y-1">
          <label htmlFor="monitoring-mode" className="block text-xs font-semibold">
            Monitoring mode
          </label>
          <select
            id="monitoring-mode"
            aria-label="Monitoring mode"
            value={mode}
            className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
            onChange={e => setMode(e.target.value as MonitoringMode)}
          >
            <option value="report_only">report_only · Observe usage and record telemetry without pausing</option>
            <option value="warn">warn · Issue warning alerts without pausing executions</option>
            <option value="checkpoint">checkpoint · Request authenticated owner checkpoint when threshold exceeded</option>
          </select>
        </div>

        {/* Multipliers */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div className="space-y-1">
            <label htmlFor="warning-multiplier" className="block text-xs font-semibold">
              Warning multiplier (e.g. 1.5x)
            </label>
            <input
              id="warning-multiplier"
              type="number"
              step="0.1"
              aria-label="Warning multiplier"
              aria-invalid={Boolean(fieldErrors.warning_multiplier)}
              className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
              value={warningMultiplier}
              onChange={e => setWarningMultiplier(e.target.value)}
            />
            {fieldErrors.warning_multiplier && (
              <p role="alert" className="text-xs text-[var(--danger)]">
                {fieldErrors.warning_multiplier}
              </p>
            )}
          </div>

          <div className="space-y-1">
            <label htmlFor="checkpoint-multiplier" className="block text-xs font-semibold">
              Checkpoint multiplier (e.g. 2.0x)
            </label>
            <input
              id="checkpoint-multiplier"
              type="number"
              step="0.1"
              aria-label="Checkpoint multiplier"
              aria-invalid={Boolean(fieldErrors.checkpoint_multiplier)}
              className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
              value={checkpointMultiplier}
              onChange={e => setCheckpointMultiplier(e.target.value)}
            />
            {fieldErrors.checkpoint_multiplier && (
              <p role="alert" className="text-xs text-[var(--danger)]">
                {fieldErrors.checkpoint_multiplier}
              </p>
            )}
          </div>
        </div>

        {/* History Window & Minimum Samples */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <div className="space-y-1">
            <label htmlFor="history-limit" className="block text-xs font-semibold">
              History limit (completions)
            </label>
            <input
              id="history-limit"
              type="text"
              inputMode="numeric"
              aria-label="History limit"
              aria-invalid={Boolean(fieldErrors.history_limit)}
              className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
              value={historyLimit}
              onChange={e => setHistoryLimit(e.target.value)}
            />
            {fieldErrors.history_limit && (
              <p role="alert" className="text-xs text-[var(--danger)]">
                {fieldErrors.history_limit}
              </p>
            )}
          </div>

          <div className="space-y-1">
            <label htmlFor="minimum-comparables" className="block text-xs font-semibold">
              Minimum comparables
            </label>
            <input
              id="minimum-comparables"
              type="text"
              inputMode="numeric"
              aria-label="Minimum comparables"
              aria-invalid={Boolean(fieldErrors.minimum_comparables)}
              className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
              value={minimumComparables}
              onChange={e => setMinimumComparables(e.target.value)}
            />
            {fieldErrors.minimum_comparables && (
              <p role="alert" className="text-xs text-[var(--danger)]">
                {fieldErrors.minimum_comparables}
              </p>
            )}
          </div>

          <div className="space-y-1">
            <label htmlFor="window-days" className="block text-xs font-semibold">
              Rolling window (days, optional)
            </label>
            <input
              id="window-days"
              type="text"
              inputMode="numeric"
              aria-label="Rolling window days"
              placeholder="All time (unbounded)"
              className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
              value={windowDays}
              onChange={e => setWindowDays(e.target.value)}
            />
            {fieldErrors.window_days && (
              <p role="alert" className="text-xs text-[var(--danger)]">
                {fieldErrors.window_days}
              </p>
            )}
          </div>
        </div>

        {/* Cross-project cohort fallback checkbox */}
        <label className="flex items-center gap-2 text-xs font-medium cursor-pointer">
          <input
            type="checkbox"
            checked={allowCrossProjectFallback}
            aria-label="Cross-project cohort fallback"
            onChange={e => setAllowCrossProjectFallback(e.target.checked)}
          />
          <span>Allow cross-project comparable cohort fallback when project history is sparse</span>
        </label>

        {/* Advanced hard caps and manual estimates */}
        <details className={styles.commandDetails}>
          <summary className={styles.commandSummary}>
            <strong>Advanced hard caps and manual estimates</strong>
            <span className="text-xs text-[var(--muted)]">
              (Optional ceilings; blank leaves tokens uncapped. Model context capacity is not a cumulative threshold.)
            </span>
          </summary>

          <div className="space-y-3 pt-3">
            <div className="space-y-1">
              <label htmlFor="hard-token-cap" className="block text-xs font-semibold">
                Optional hard token cap
              </label>
              <input
                id="hard-token-cap"
                type="text"
                inputMode="numeric"
                placeholder="Unbounded (no hard cap)"
                aria-label="Optional hard token cap"
                aria-invalid={Boolean(fieldErrors.hard_token_cap)}
                className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
                value={hardTokenCap}
                onChange={e => setHardTokenCap(e.target.value)}
              />
              {fieldErrors.hard_token_cap && (
                <p role="alert" className="text-xs text-[var(--danger)]">
                  {fieldErrors.hard_token_cap}
                </p>
              )}
            </div>

            <div className="space-y-1">
              <label htmlFor="hard-context-cap" className="block text-xs font-semibold">
                Optional hard context cap
              </label>
              <input
                id="hard-context-cap"
                type="text"
                inputMode="numeric"
                placeholder="Unbounded (no hard cap)"
                aria-label="Optional hard context cap"
                aria-invalid={Boolean(fieldErrors.hard_context_cap)}
                className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
                value={hardContextCap}
                onChange={e => setHardContextCap(e.target.value)}
              />
              {fieldErrors.hard_context_cap && (
                <p role="alert" className="text-xs text-[var(--danger)]">
                  {fieldErrors.hard_context_cap}
                </p>
              )}
            </div>

            <div className="space-y-1">
              <label htmlFor="manual-estimate-tokens" className="block text-xs font-semibold">
                Manual estimate tokens (optional override)
              </label>
              <input
                id="manual-estimate-tokens"
                type="text"
                inputMode="numeric"
                placeholder="None (uses learned baseline)"
                aria-label="Manual estimate tokens"
                aria-invalid={Boolean(fieldErrors.manual_estimate_tokens)}
                className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
                value={manualEstimateTokens}
                onChange={e => setManualEstimateTokens(e.target.value)}
              />
              {fieldErrors.manual_estimate_tokens && (
                <p role="alert" className="text-xs text-[var(--danger)]">
                  {fieldErrors.manual_estimate_tokens}
                </p>
              )}
            </div>
          </div>
        </details>

        {/* Optional Owner Note */}
        <div className="space-y-1">
          <label htmlFor="monitoring-note" className="block text-xs font-semibold">
            Optional owner note
          </label>
          <textarea
            id="monitoring-note"
            rows={2}
            placeholder="Reason for revision (optional)..."
            aria-label="Optional owner note"
            className="w-full px-3 py-1.5 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)]"
            value={note}
            onChange={e => setNote(e.target.value)}
          />
        </div>

        {/* Actions: direct owner action, no extra approval step */}
        <div className="flex gap-2 pt-2">
          <Button type="submit" variant="primary" busy={saving} disabled={saving}>
            {saving ? 'Saving…' : 'Save monitoring policy'}
          </Button>
          {onCancel && (
            <Button type="button" variant="quiet" disabled={saving} onClick={onCancel}>
              Cancel
            </Button>
          )}
        </div>
      </fieldset>
    </form>
  );
}
