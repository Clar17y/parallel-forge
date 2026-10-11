'use client';

import { Button } from '@/components/ui/button';
import type {
  MonitoringPolicyResponse,
  TaskUsageHistoryResponse,
} from '@/lib/api/monitoring-policy';
import styles from './project-policy.module.css';

export interface MonitoringPolicySummaryProps {
  policy: MonitoringPolicyResponse | null;
  report?: TaskUsageHistoryResponse | null;
  loadingReport?: boolean;
  onEdit?: () => void;
}

export function MonitoringPolicySummary({
  policy,
  report,
  loadingReport = false,
  onEdit,
}: MonitoringPolicySummaryProps) {
  const overview = report?.overview;
  const filters = report?.filters;

  return (
    <section className={styles.setupSection} aria-label="Monitoring policy summary">
      <div className={styles.setupSectionHeader}>
        <h2 className={styles.setupSectionTitle}>Task usage monitoring policy</h2>
        {onEdit && (
          <Button type="button" variant="secondary" onClick={onEdit}>
            {policy ? 'Edit monitoring policy' : 'Configure monitoring policy'}
          </Button>
        )}
      </div>

      {!policy ? (
        <div className="space-y-2 text-sm text-[var(--muted-foreground)]">
          <p>
            No monitoring policy revision configured. Configure monitoring to see its current behavior and optional hard caps.
          </p>
        </div>
      ) : (
        <div className="space-y-4 text-sm">
          <p className="text-xs text-[var(--muted)]">Saved policy settings</p>
          <div className={styles.metaRow}>
            <span>Revision {policy.revision}</span>
            <span>Mode: {policy.mode}</span>
            <span>Warning threshold: {policy.warning_multiplier}x</span>
            <span>Checkpoint threshold: {policy.checkpoint_multiplier}x</span>
            <span>History window: {policy.history_limit} completions</span>
            <span>Minimum comparables: {policy.minimum_comparables} samples</span>
            <span>Time window: {policy.window_days != null ? `${policy.window_days} days` : 'All time (unbounded)'}</span>
            <span>Cross-project fallback: {policy.allow_cross_project_fallback ? 'Enabled' : 'Disabled'}</span>
            <span>Hard token cap: {policy.hard_token_cap != null ? `${policy.hard_token_cap.toLocaleString()} tokens` : 'Unbounded (no cap)'}</span>
            <span>Hard context cap: {policy.hard_context_cap != null ? `${policy.hard_context_cap.toLocaleString()} tokens` : 'Unbounded (no cap)'}</span>
            <span>Manual estimate: {policy.manual_estimate_tokens != null ? `${policy.manual_estimate_tokens.toLocaleString()} tokens` : 'None (use a learned reference when available)'}</span>
          </div>

          {policy.owner_action && (
            <div className="p-3 bg-[var(--surface-muted)] border border-[var(--border)] rounded text-xs space-y-1">
              <p className="font-semibold text-[var(--foreground)]">Audit Receipt</p>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-[var(--muted-foreground)]">
                <span>action: {policy.owner_action.action}</span>
                <span>actor: {policy.owner_action.actor_id}</span>
                <span>snapshot: {policy.owner_action.affected_snapshot}</span>
                <span>Replayed: {policy.owner_action.replayed ? 'Yes' : 'No'}</span>
              </div>
              {policy.owner_action.warnings && policy.owner_action.warnings.length > 0 && (
                <div role="alert" className="text-[var(--warning)] mt-1">
                  {policy.owner_action.warnings.map((w, idx) => (
                    <p key={idx}>{w}</p>
                  ))}
                </div>
              )}
            </div>
          )}

          {loadingReport && <p className="text-xs text-[var(--muted)]">Loading advisory usage history…</p>}
          {!loadingReport && (!overview || !filters) && <p className="text-xs text-[var(--muted)]">Usage report unavailable; readiness unknown.</p>}

          {overview && filters && (
            <div className="p-3 bg-[var(--surface-muted)] border border-[var(--border)] rounded space-y-2">
              <div className="flex items-center justify-between">
                <span className="font-semibold text-xs text-[var(--foreground)]">Project-wide measured usage history</span>
                <span className="text-xs text-[var(--muted)]">Reported source: {overview.cohort_source}</span>
              </div>
              <p className="text-xs text-[var(--muted)]">This project-wide report can mix task types, models, routes, and subjects. Its overview does not establish a comparable baseline for a specific task.</p>
              <p className="text-xs text-[var(--muted)]">
                Report sampling: {filters.history_limit} completions; minimum {filters.minimum_comparables} samples; {filters.window_days == null ? 'all time' : `${filters.window_days} days`}; projects {filters.project_ids.length ? filters.project_ids.join(', ') : 'all authorized'}; subject {filters.subject_kind ?? 'all'}; task {filters.task_kind ?? 'all'}; provider {filters.provider ?? 'all'}; model {filters.model ?? 'all'}; effort {filters.effort ?? 'all'}.
              </p>

              {overview.status === 'ready' && (
                <div className="space-y-2">
                  <p className="text-xs text-[var(--muted-foreground)]">Report status: ready for these sampling rules (minimum {filters.minimum_comparables} samples).</p>
                  <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 text-xs">
                    <div>
                      <span className="text-[var(--muted)] block">Measured mean:</span>
                      <span className="font-medium text-[var(--foreground)]">
                        {overview.mean_tokens != null ? `${overview.mean_tokens.toLocaleString()} tokens` : 'Unknown'}
                      </span>
                    </div>
                    <div>
                      <span className="text-[var(--muted)] block">Median:</span>
                      <span className="font-medium text-[var(--foreground)]">
                        {overview.median_tokens != null ? `${overview.median_tokens.toLocaleString()} tokens` : 'Unknown'}
                      </span>
                    </div>
                    <div>
                      <span className="text-[var(--muted)] block">P90:</span>
                      <span className="font-medium text-[var(--foreground)]">
                        {overview.p90_tokens != null ? `${overview.p90_tokens.toLocaleString()} tokens` : 'Unknown'}
                      </span>
                    </div>
                    <div>
                      <span className="text-[var(--muted)] block">Sample count:</span>
                      <span className="font-medium text-[var(--foreground)]">
                        {overview.sample_count} eligible completions
                      </span>
                    </div>
                    <div>
                      <span className="text-[var(--muted)] block">Coverage:</span>
                      <span className="font-medium text-[var(--foreground)]">
                        {overview.measured_coverage != null
                          ? `${Math.round(overview.measured_coverage * 100)}%`
                          : `${overview.coverage_count} / ${overview.eligible_count}`}
                      </span>
                    </div>
                  </div>
                </div>
              )}

              {overview.status === 'insufficient_history' && (
                <div className="space-y-1 text-xs">
                  <p className="text-[var(--muted-foreground)]">
                    Insufficient history: {overview.sample_count} / {filters.minimum_comparables} reported samples under report filters. Advisory reference unavailable.
                  </p>
                  <p className="text-[var(--muted-foreground)]">
                    {overview.mean_tokens === 0
                      ? 'Measured average: 0 tokens (measured zero)'
                      : overview.mean_tokens == null
                      ? 'Measured average: Unknown (no samples yet)'
                      : `Measured average: ${overview.mean_tokens.toLocaleString()} tokens`}
                  </p>
                </div>
              )}

              {overview.status === 'manual' && (
                <div className="text-xs text-[var(--muted-foreground)]">
                  Manual estimate in use: {overview.reference_tokens != null ? `${overview.reference_tokens.toLocaleString()} tokens` : 'Unknown'}. This is an owner estimate, not a measured average.
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
