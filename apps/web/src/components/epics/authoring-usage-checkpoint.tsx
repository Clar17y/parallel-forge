'use client';

import { useEffect, useRef, useState } from 'react';
import { ApiError, mutate } from '@/lib/api/client';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import type { MonitoringPolicyResponse } from '@/lib/api/monitoring-policy';
import { retainedPolicyIntegersAreSafe } from '@/components/task-usage/retained-policy-integers';

type CheckpointResponse = components['schemas']['TaskUsageCheckpointResponse'];
type CommandRequest = components['schemas']['TaskUsageOwnerCommandRequest'];
type CommandResponse = components['schemas']['TaskUsageOwnerCommandResponse'];
type Report = components['schemas']['UsageReport'];
type Policy = components['schemas']['MonitoringPolicy'];
type Pending = { body: CommandRequest; key: string };
const value = (tokens: number | null | undefined) => tokens == null ? 'Unknown' : Number.isInteger(tokens) && !Number.isSafeInteger(tokens) ? 'Too large to display precisely' : tokens.toLocaleString();

export function AuthoringUsageCheckpoint({ epicId, projectId, jobId, kind, locked }: {
  epicId: string; projectId: string; jobId: string; kind: 'brainstorm' | 'decomposition'; locked: boolean;
}) {
  const base = `/epics/${epicId}/${kind === 'brainstorm' ? 'brainstorm' : 'decomposition'}-jobs/${jobId}/usage-checkpoint`;
  const projectQuery = `?project_id=${encodeURIComponent(projectId)}`;
  const data = useApi<CheckpointResponse>(`${base}${projectQuery}`, { refreshIntervalMs: 5000, keepPreviousOnRefresh: true });
  const [retry, setRetry] = useState<Pending | null>(null);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState('');
  const [receipt, setReceipt] = useState<CommandResponse['receipt'] | null>(null);
  const [showPolicy, setShowPolicy] = useState(false);
  const [mode, setMode] = useState<components['schemas']['MonitoringMode']>('checkpoint');
  const [warning, setWarning] = useState('1.5');
  const [threshold, setThreshold] = useState('2');
  const running = useRef(false);
  const lifecycle = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    lifecycle.current = controller;
    return () => { controller.abort(); lifecycle.current = null; };
  }, []);
  const checkpoint = data.value?.checkpoint;
  const report = data.value?.report;
  const reportProjectId = report?.binding.subject.project_id ?? checkpoint?.binding.subject.project_id ?? projectId;
  const currentPolicy = useApi<MonitoringPolicyResponse | null>(reportProjectId ? `/projects/${reportProjectId}/monitoring-policy` : null, { keepPreviousOnRefresh: true, keepPreviousOnError: true });
  const policy = currentPolicy.value ?? null;
  const warningValue = Number(warning);
  const thresholdValue = Number(threshold);
  const policyValid = !!policy && Number.isFinite(warningValue) && warningValue >= 1
    && Number.isFinite(thresholdValue) && thresholdValue >= warningValue && Number.isSafeInteger(policy.revision + 1)
    && retainedPolicyIntegersAreSafe(policy);
  const settled = checkpoint?.state === 'paused' && checkpoint.process_settled;
  const knownDimensions = [report?.input_tokens, report?.output_tokens].filter((value): value is number => value != null);
  const knownTotal = knownDimensions.reduce((sum, value) => sum + BigInt(Number.isSafeInteger(value) && value >= 0 ? value : 0), 0n);
  const totalIsSafe = !!report && ['complete', 'lower_bound'].includes(report.completeness) && knownDimensions.length > 0
    && knownDimensions.every(value => Number.isSafeInteger(value) && value >= 0)
    && knownTotal <= BigInt(Number.MAX_SAFE_INTEGER);

  async function submit(body: CommandRequest) {
    if (running.current || !checkpoint || !lifecycle.current) return;
    const selected = retry ?? { body, key: body.idempotency_key };
    const signal = lifecycle.current.signal;
    running.current = true; setPending(true); setRetry(selected); setMessage(''); setReceipt(null);
    try {
      const result = await mutate<CommandResponse>(`${base}/commands${projectQuery}`, selected.body as unknown as Record<string, unknown>, { idempotencyKey: selected.key, signal });
      if (!result || result.receipt.checkpoint_id !== selected.body.checkpoint_id || result.receipt.action !== selected.body.action
        || result.checkpoint.checkpoint_id !== selected.body.checkpoint_id || !Number.isSafeInteger(result.receipt.result_version)
        || result.receipt.result_version < selected.body.expected_version + 1 || result.receipt.result_version > result.checkpoint.version) throw new Error('No matching durable receipt');
      if (signal.aborted) return;
      setRetry(null); setReceipt(result.receipt); setMessage('Owner command recorded. The authoring scheduler will recheck independent controls before continuing.');
      data.refresh();
      if (selected.body.action === 'set_policy') {
        currentPolicy.refresh();
      }
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof ApiError && error.status === 409) {
        setRetry(null); setMessage('Checkpoint changed; current state refreshed. Review before acting again.');
        data.refresh();
        currentPolicy.refresh();
      }
      else if (error instanceof ApiError && [401, 403, 422].includes(error.status)) { setRetry(null); setMessage('Owner command was rejected. Check the operator session and current checkpoint state.'); data.refresh(); }
      else setMessage('The result is uncertain. Retry the same request to retrieve its durable receipt.');
    } finally { running.current = false; if (!signal.aborted) setPending(false); }
  }

  function command(action: CommandRequest['action'], continueMode?: CommandRequest['continue_mode']) {
    if (!checkpoint) return;
    const key = crypto.randomUUID();
    void submit({ checkpoint_id: checkpoint.checkpoint_id, expected_version: checkpoint.version, idempotency_key: key,
      action, continue_mode: continueMode ?? null, requested_policy: null, override_note: null });
  }

  function changePolicy() {
    if (!checkpoint || !policy || !policyValid || !reportProjectId) return;
    const requested: Policy = { project_id: reportProjectId, revision: policy.revision + 1, mode,
      warning_multiplier: warningValue, checkpoint_multiplier: thresholdValue, history_limit: policy.history_limit,
      minimum_comparables: policy.minimum_comparables, window_days: policy.window_days,
      allow_cross_project_fallback: policy.allow_cross_project_fallback, manual_estimate_tokens: policy.manual_estimate_tokens,
      hard_token_cap: policy.hard_token_cap, hard_context_cap: policy.hard_context_cap };
    const key = crypto.randomUUID();
    void submit({ checkpoint_id: checkpoint.checkpoint_id, expected_version: checkpoint.version, idempotency_key: key,
      action: 'set_policy', continue_mode: null, requested_policy: requested, override_note: null });
    setShowPolicy(false);
  }

  return <section aria-label="Authoring usage checkpoint" className="mt-3 space-y-2 rounded border border-[var(--border)] p-3">
    <div className="flex items-center justify-between gap-3"><h4 className="font-semibold">Usage monitoring</h4>
      <button type="button" disabled={data.refreshing} onClick={data.refresh}>Refresh authoring usage</button></div>
    {data.loading ? <p role="status">Loading authoring usage…</p> : null}
    {data.failed ? <p role="alert">Authoring usage is unavailable. <button type="button" onClick={data.refresh}>Retry</button></p> : null}
    {data.value ? <>
      <p>{checkpoint ? `Checkpoint ${checkpoint.state} · generation ${value(checkpoint.generation)} · version ${value(checkpoint.version)} · ${checkpoint.process_settled ? 'process settled' : 'process settlement pending'}` : 'No usage checkpoint is recorded for this authoring job.'}</p>
      {report ? <AuthoringReport report={report} /> : <p>Measured usage and frozen reference: Unknown.</p>}
      {report?.completeness === 'lower_bound' ? <p>Next checkpoint uses the known measured lower bound; unknown token dimensions remain Unknown.</p> : null}
      {settled ? <div className="flex flex-wrap gap-2">
        <button type="button" disabled={pending || !!retry || locked || !totalIsSafe || report?.baseline == null}
          onClick={() => command('continue', 'next_checkpoint')}>Continue to next checkpoint</button>
        <button type="button" disabled={pending || !!retry || locked} onClick={() => command('continue', 'without_usage_pauses')}>Continue without usage pauses</button>
        <button type="button" disabled={pending || !!retry || locked} onClick={() => {
          if (policy) { setMode(['checkpoint', 'warn', 'report_only'].includes(policy.mode) ? policy.mode as typeof mode : 'checkpoint'); setWarning(String(policy.warning_multiplier)); setThreshold(String(policy.checkpoint_multiplier)); setShowPolicy(open => !open); }
        }}>Change monitoring settings</button>
        <button type="button" disabled={pending || !!retry || locked} onClick={() => command('stop')}>Stop authoring job</button>
      </div> : null}
      {settled && showPolicy && policy ? <fieldset className="space-y-2 rounded border border-[var(--border)] p-3" disabled={pending || !!retry || locked}>
        <legend className="font-semibold">New monitoring revision</legend>
        {policy.hard_token_cap != null || policy.hard_context_cap != null ? <p>Existing explicit hard caps are retained unchanged.</p> : <p>No explicit hard cap is configured; this changes monitoring only.</p>}
        <label className="grid gap-1">Monitoring mode<select value={mode} onChange={event => setMode(event.target.value as typeof mode)}><option value="report_only">Report only</option><option value="warn">Warn</option><option value="checkpoint">Checkpoint</option></select></label>
        <label className="grid gap-1">Warning multiplier<input type="number" min="1" step="0.1" value={warning} onChange={event => setWarning(event.target.value)} /></label>
        <label className="grid gap-1">Checkpoint multiplier<input type="number" min="1" step="0.1" value={threshold} onChange={event => setThreshold(event.target.value)} /></label>
        {policy && !retainedPolicyIntegersAreSafe(policy) ? <p role="alert">Cannot save this monitoring revision because an existing manual estimate or hard cap exceeds safe browser integer precision; it has not been changed. Use POST /api/projects/{reportProjectId}/monitoring-policy with a client that preserves signed 64-bit integers.</p> : !policyValid ? <p role="alert">Multipliers must be finite and at least 1; checkpoint cannot be below warning.</p> : null}
        <button type="button" disabled={pending || !!retry || locked || !policyValid} onClick={changePolicy}>Save monitoring revision</button>
      </fieldset> : null}
    </> : null}
    {retry ? <button type="button" disabled={pending} onClick={() => void submit(retry.body)}>Retry same request</button> : null}
    {pending ? <p role="status">Recording owner command…</p> : null}{message ? <p role="status">{message}</p> : null}
    {receipt ? <p>Receipt: {receipt.action} · {receipt.state} · version {value(receipt.result_version)} · {receipt.replayed ? 'replayed' : 'new owner action'}</p> : null}
  </section>;
}

function AuthoringReport({ report }: { report: Report }) {
  const baseline = report.baseline;
  return <div className="space-y-1">
    <p>Input {value(report.input_tokens)} · output {value(report.output_tokens)} · cached input {value(report.cached_input_tokens)} · reasoning output {value(report.reasoning_output_tokens)}</p>
    <p>{report.completeness === 'complete' ? 'Complete' : report.completeness === 'lower_bound' ? 'Lower bound' : 'Completeness unknown'} · {report.freshness} measurement</p>
    <p>Current occupancy {value(report.context_occupied_tokens)} · peak occupancy {value(report.context_peak_tokens)} · capacity {value(report.context_capacity_tokens)} · observed compactions {value(report.compaction_count)}</p>
    <p>Phase {report.binding.admitted_phase_id} · source {report.binding.source.kind} · route {report.binding.effective_route.members.map(item => `${item.role} ${item.provider}/${item.model}/${item.reasoning_effort ?? 'effort unknown'}`).join(', ') || 'Unknown'}</p>
    {report.sources.length ? <ul aria-label="Observed source usage" className="list-disc pl-5">{report.sources.map((source, index) =>
      <li key={`${source.binding.source.source_id}:${index}`}>{source.phase} · {source.role} · {source.delegated === null ? 'ancestry unknown' : source.delegated ? 'delegated' : 'primary'} · input {value(source.input_tokens)} · output {value(source.output_tokens)} · occupancy {value(source.context_occupied_tokens)} · peak {value(source.context_peak_tokens)} · compactions {value(source.compaction_count)} · {source.completeness}</li>)}</ul> : <p>Per-source phase, role and delegation detail: Unknown.</p>}
    {baseline ? <p>{baseline.status === 'manual' ? 'Manual estimate' : 'Frozen learned reference'} {value(baseline.reference_tokens)} · mean {value(baseline.mean_tokens)} · median {value(baseline.median_tokens)} · P90 {value(baseline.p90_tokens)} · {value(baseline.sample_count)} samples · {value(baseline.coverage_count)} measured sources · {baseline.comparison_scope}</p> : <p>Frozen reference: Unknown</p>}
  </div>;
}
