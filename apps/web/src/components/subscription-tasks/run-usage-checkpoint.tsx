'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { ApiError, mutate } from '@/lib/api/client';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import type { MonitoringPolicyResponse } from '@/lib/api/monitoring-policy';
import { retainedPolicyIntegersAreSafe, knownReportTotal } from '@/components/task-usage/retained-policy-integers';
import { matchesOwnerReceipt, nextVersion, versionValue } from '@/components/task-usage/owner-version';
import { MAX_SIGNED_64 } from '@/lib/api/exact-integer';

type CheckpointResponse = components['schemas']['TaskUsageCheckpointResponse'];
type CommandRequest = components['schemas']['TaskUsageOwnerCommandRequest'];
type CommandResponse = components['schemas']['TaskUsageOwnerCommandResponse'];
type Checkpoint = components['schemas']['CheckpointRecord'];
type Report = components['schemas']['UsageReport'];
type Mode = components['schemas']['MonitoringMode'];
type Policy = components['schemas']['MonitoringPolicy'];
type PendingCommand = { key: string; body: CommandRequest };
const stateNames: Record<Checkpoint['state'], string> = {
  checkpoint_requested: 'Checkpoint requested', pausing: 'Pausing', paused: 'Paused',
  resumed: 'Resumed', resolved: 'Resolved',
};
const tokenValue = (value: number | string | bigint | null | undefined) => {
  if (value == null) return 'Unknown';
  if (typeof value === 'bigint') return value.toLocaleString();
  if (typeof value === 'string') {
    try {
      return BigInt(value).toLocaleString();
    } catch {
      return value;
    }
  }
  if (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value))) return 'Too large to display precisely';
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
};
function knownTotal(report: Report | null | undefined): bigint | null {
  return knownReportTotal(report);
}
function isMode(value: string): value is Mode { return value === 'report_only' || value === 'warn' || value === 'checkpoint'; }

export function RunUsageCheckpoint({ runId, runAllowsExecution, runIsTerminal, quotaBlocked, manualPause }: {
  runId: string; runAllowsExecution: boolean; runIsTerminal: boolean; quotaBlocked: boolean; manualPause: boolean;
}) {
  const data = useApi<CheckpointResponse>(`/runs/${runId}/usage-checkpoint`, { refreshIntervalMs: 5000, keepPreviousOnRefresh: true });
  const checkpoint = data.value?.checkpoint;
  const report = data.value?.report;
  const projectId = report?.binding.subject.project_id ?? checkpoint?.binding.subject.project_id ?? null;
  const policy = useApi<MonitoringPolicyResponse | null>(projectId ? `/projects/${projectId}/monitoring-policy` : null,
    { keepPreviousOnRefresh: true, keepPreviousOnError: true });
  const [binding, setBinding] = useState<PendingCommand | null>(null);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState('');
  const [showPolicy, setShowPolicy] = useState(false);
  const [mode, setMode] = useState<Mode>('checkpoint');
  const [warning, setWarning] = useState('1.5');
  const [threshold, setThreshold] = useState('2');
  const [note, setNote] = useState('');
  const [receipt, setReceipt] = useState<CommandResponse['receipt'] | null>(null);
  const busy = useRef(false);
  const lifecycle = useRef<AbortController | null>(null);
  useEffect(() => { const controller = new AbortController(); lifecycle.current = controller; return () => controller.abort(); }, []);

  const settled = checkpoint?.state === 'paused' && checkpoint.process_settled;
  const versionCanAdvance = checkpoint != null && versionValue(checkpoint.version, MAX_SIGNED_64 - 1n) !== null;
  const total = knownTotal(report);
  // The owner is recording a monitoring choice. Provider quota is checked again
  // by scheduler admission and must not prevent recording that choice.
  const canContinue = settled && versionCanAdvance && runAllowsExecution && !runIsTerminal && !manualPause;
  const canNext = canContinue && total !== null && report?.baseline != null;
  const canBypass = canContinue;
  const currentPolicy = policy.value ?? null;
  const policyRevisionCanAdvance = currentPolicy !== null && nextVersion(currentPolicy.revision) !== null;
  const modeIsValid = currentPolicy !== null && isMode(currentPolicy.mode);
  const warnValue = Number(warning);
  const thresholdValue = Number(threshold);
  const policyValid = modeIsValid && Number.isFinite(warnValue) && warnValue >= 1
    && Number.isFinite(thresholdValue) && thresholdValue >= warnValue
    && !!currentPolicy && retainedPolicyIntegersAreSafe(currentPolicy)
    && policyRevisionCanAdvance;
  const canChangePolicy = settled && versionCanAdvance && !runIsTerminal && !!currentPolicy;
  const canStop = settled && versionCanAdvance && !runIsTerminal;

  async function submit(body: CommandRequest) {
    if (busy.current || !checkpoint || !lifecycle.current) return;
    const selected = binding ?? { key: body.idempotency_key, body };
    const signal = lifecycle.current.signal;
    busy.current = true; setPending(true); setBinding(selected); setMessage(''); setReceipt(null);
    try {
      const result = await mutate<CommandResponse>(`/runs/${runId}/usage-checkpoint/commands`, selected.body as unknown as Record<string, unknown>,
        { idempotencyKey: selected.key, signal });
      if (!result || !matchesOwnerReceipt(selected.body, result)) {
        throw new Error('Owner command receipt unavailable');
      }
      if (!signal.aborted) {
        setBinding(null); setReceipt(result.receipt); setNote('');
        setMessage(selected.body.action === 'continue'
          ? quotaBlocked
            ? 'Continue recorded. The scheduler will wait for provider quota eligibility and recheck other independent controls before admitting work.'
            : 'Continue recorded. The scheduler will recheck provider quota eligibility and other independent controls before admitting work.'
          : selected.body.action === 'stop'
            ? 'Run stop requested. Wait for normal cancellation settlement before treating work as stopped.'
            : 'Monitoring policy revision recorded.');
        data.refresh();
        if (selected.body.action === 'set_policy') {
          policy.refresh();
        }
      }
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof ApiError && error.status === 409) {
        setBinding(null); setMessage('The checkpoint changed. Current state has been refreshed; review it before acting again.');
        data.refresh();
        policy.refresh();
      } else if (error instanceof ApiError && [401, 403, 422].includes(error.status)) {
        setBinding(null); setMessage('The owner command was rejected. Check the operator session and current checkpoint state.'); data.refresh();
      } else setMessage('The command could not be confirmed. Retry the same request to retrieve its durable receipt.');
    } finally { busy.current = false; if (!signal.aborted) setPending(false); }
  }

  function command(action: CommandRequest['action'], continueMode?: CommandRequest['continue_mode'], requestedPolicy?: Policy) {
    if (!checkpoint) return;
    if (versionValue(checkpoint.version, MAX_SIGNED_64 - 1n) === null) return;
    const key = crypto.randomUUID();
    void submit({ checkpoint_id: checkpoint.checkpoint_id, expected_version: checkpoint.version,
      idempotency_key: key, action, continue_mode: continueMode ?? null, requested_policy: requestedPolicy ?? null,
      override_note: note.trim() || null });
  }

  function changePolicy() {
    if (!currentPolicy || !isMode(currentPolicy.mode) || !policyValid || !projectId) return;
    const revision = nextVersion(currentPolicy.revision);
    if (revision === null) return;
    const requested: Policy = {
      project_id: projectId, revision, mode,
      warning_multiplier: warnValue, checkpoint_multiplier: thresholdValue,
      history_limit: currentPolicy.history_limit, minimum_comparables: currentPolicy.minimum_comparables,
      window_days: currentPolicy.window_days, allow_cross_project_fallback: currentPolicy.allow_cross_project_fallback,
      manual_estimate_tokens: currentPolicy.manual_estimate_tokens, hard_token_cap: currentPolicy.hard_token_cap,
      hard_context_cap: currentPolicy.hard_context_cap,
    };
    command('set_policy', null, requested);
    setShowPolicy(false);
  }

  const retry = () => { if (binding) void submit(binding.body); };
  return <section aria-label="Run usage checkpoint" className="my-4 space-y-3 rounded-lg border border-slate-300 p-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h3 className="font-semibold">Usage monitoring</h3><p>Measured task usage and monitoring checkpoint. This status is separate from provider failures and optional hard caps.</p></div>
      <button type="button" onClick={data.refresh} disabled={data.refreshing} className="rounded border border-slate-400 px-3 py-2">Refresh monitoring</button>
    </div>
    {data.loading ? <p role="status">Loading usage monitoring…</p> : null}
    {data.failed ? <p role="alert">Usage monitoring is unavailable. <button type="button" onClick={data.refresh}>Retry</button></p> : null}
    {data.value ? <>
      {checkpoint ? <div className="space-y-1 rounded border border-slate-300 p-3">
        <p>Checkpoint status: {stateNames[checkpoint.state]} · generation {tokenValue(checkpoint.generation)} · version {tokenValue(checkpoint.version)}</p>
        <p>{checkpoint.process_settled ? 'Process settled' : 'Process settlement pending'}</p>
        <p>Frozen reference digest: {checkpoint.reference_digest}</p>
        <p>Frozen policy revision: {tokenValue(checkpoint.policy_revision)} · current owner monitoring revision: {tokenValue(checkpoint.monitoring_policy_revision ?? report?.policy_revision)}</p>
        {checkpoint.next_checkpoint_threshold_tokens != null ? <p>Next usage checkpoint: {tokenValue(checkpoint.next_checkpoint_threshold_tokens)} tokens</p> : null}
        {checkpoint.usage_pauses_disabled ? <p>Further usage checkpoints are disabled by the owner.</p> : null}
      </div> : <p>No usage checkpoint has been recorded for this run.</p>}
      {report ? <UsageDetails report={report} /> : <p>Usage totals and frozen reference are unknown; no report is available.</p>}
      {report?.warning ? <p role="status">Monitoring warning: {report.warning}</p> : null}
      {checkpoint && checkpoint.state === 'paused' && !checkpoint.process_settled ? <p role="alert">Owner continuation is unavailable until process and effects have settled.</p> : null}
      {checkpoint && settled && !versionCanAdvance ? <p role="alert">Checkpoint version cannot be incremented within signed 64-bit storage. Owner actions are unavailable.</p> : null}
      {manualPause ? <p role="status">An independent manual task pause remains active. Monitoring continuation cannot clear it.</p> : null}
      {!runAllowsExecution && !runIsTerminal ? <p role="status">Run controls currently block execution. Resolve the manual pause or cancellation through the existing run controls first.</p> : null}
      {quotaBlocked ? <p role="status">A subscription task on this page is waiting for provider quota eligibility. Recording a monitoring choice does not clear the quota wait.</p> : null}
      {runIsTerminal ? <p role="status">The run is terminal; monitoring controls cannot resume it.</p> : null}
      {checkpoint && settled ? <>
        <p>Owner controls apply directly to this checkpoint; a note is optional.</p>
        <label className="grid gap-1">Optional note<textarea rows={2} value={note} disabled={pending || !!binding} onChange={event => setNote(event.target.value)} className="rounded border border-slate-400 p-2" /></label>
        <div className="flex flex-wrap gap-2">
          <button type="button" disabled={pending || !!binding || !canNext} onClick={() => command('continue', 'next_checkpoint')} className="rounded border border-slate-400 px-3 py-2">Continue to next checkpoint</button>
          <button type="button" disabled={pending || !!binding || !canBypass} onClick={() => command('continue', 'without_usage_pauses')} className="rounded border border-slate-400 px-3 py-2">Continue without usage pauses</button>
          <button type="button" disabled={pending || !!binding || !canChangePolicy} onClick={() => {
            if (currentPolicy && isMode(currentPolicy.mode)) {
              setMode(currentPolicy.mode); setWarning(String(currentPolicy.warning_multiplier));
              setThreshold(String(currentPolicy.checkpoint_multiplier)); setShowPolicy(value => !value);
            }
          }} className="rounded border border-slate-400 px-3 py-2">Change monitoring settings</button>
          <button type="button" disabled={pending || !!binding || !canStop} onClick={() => command('stop')} className="rounded border border-slate-400 px-3 py-2">Stop run</button>
        </div>
        {report?.baseline == null || total === null ? <p>Continue to next checkpoint is unavailable because no safe measured total and frozen reference are available. Bypass remains an owner choice after settlement.</p> : total !== null && report?.completeness === 'lower_bound' ? <p>Next checkpoint uses the known measured lower bound; unknown token dimensions remain Unknown.</p> : null}
        {showPolicy ? <fieldset className="space-y-2 rounded border border-slate-300 p-3" disabled={pending || !!binding}>
          <legend className="font-semibold">New monitoring revision</legend>
          {currentPolicy?.hard_token_cap != null || currentPolicy?.hard_context_cap != null ? <p>Existing explicit hard caps are retained unchanged by this monitoring revision.</p> : <p>No explicit hard cap is configured; this revision changes monitoring only.</p>}
          <label className="grid gap-1">Monitoring mode<select value={mode} onChange={event => setMode(event.target.value as Mode)} className="rounded border border-slate-400 p-2">
            <option value="report_only">Report only</option><option value="warn">Warn</option><option value="checkpoint">Checkpoint</option>
          </select></label>
          <label className="grid gap-1">Warning multiplier<input type="number" min="1" step="0.1" value={warning} onChange={event => setWarning(event.target.value)} className="rounded border border-slate-400 p-2" /></label>
          <label className="grid gap-1">Checkpoint multiplier<input type="number" min="1" step="0.1" value={threshold} aria-invalid={!policyValid} onChange={event => setThreshold(event.target.value)} className="rounded border border-slate-400 p-2" /></label>
          {!policyRevisionCanAdvance ? <p role="alert">Monitoring revision cannot be incremented within signed 64-bit storage.</p> : currentPolicy && !retainedPolicyIntegersAreSafe(currentPolicy) ? <p role="alert">Cannot save this monitoring revision because an existing manual estimate or hard cap exceeds safe browser integer precision; it has not been changed. Use POST /api/projects/{projectId}/monitoring-policy with a client that preserves signed 64-bit integers.</p> : !policyValid ? <p role="alert">Multipliers must be finite, at least 1, and the checkpoint must be at or above the warning level.</p> : null}
          <button type="button" disabled={pending || !!binding || !policyValid} onClick={changePolicy} className="rounded border border-slate-400 px-3 py-2">Save monitoring revision</button>
          <Link href={`/projects/${projectId}`}>Open project policy and hard-cap settings</Link>
        </fieldset> : null}
      </> : null}
    </> : null}
    {binding ? <button type="button" disabled={pending} onClick={retry} className="rounded border border-slate-400 px-3 py-2">Retry same request</button> : null}
    {pending ? <p role="status">Recording owner command…</p> : null}
    {message ? <p role="status">{message}</p> : null}
    {receipt ? <p>Receipt: {receipt.action} · {receipt.state} · version {tokenValue(receipt.result_version)} · {receipt.replayed ? 'replayed' : 'new owner action'}</p> : null}
  </section>;
}

function UsageDetails({ report }: { report: Report }) {
  const baseline = report.baseline;
  return <div className="space-y-2 rounded border border-slate-300 p-3">
    <h4 className="font-semibold">Current measured usage</h4>
    <p>Input tokens: {tokenValue(report.input_tokens)} · Output tokens: {tokenValue(report.output_tokens)} · Cached input subset: {tokenValue(report.cached_input_tokens)} · Reasoning output subset: {tokenValue(report.reasoning_output_tokens)}</p>
    <p>Measurement: {report.completeness === 'complete' ? 'Complete' : report.completeness === 'lower_bound' ? 'Usage is a lower bound' : 'Unknown'} · Freshness: {report.freshness === 'live' ? 'Live' : report.freshness === 'final' ? 'Final' : report.freshness === 'stale' ? 'Stale' : 'Unknown'}</p>
    <p>Context occupancy: {tokenValue(report.context_occupied_tokens)} · Peak occupancy: {tokenValue(report.context_peak_tokens)} · Capacity: {tokenValue(report.context_capacity_tokens)} · Observed compactions: {tokenValue(report.compaction_count)}</p>
    <p>Phase: {report.binding.admitted_phase_id} · source: {report.binding.source.kind} · configured route: {routeNames(report.binding.configured_route.members)} · effective route: {routeNames(report.binding.effective_route.members)}</p>
    {report.sources.length ? <ul aria-label="Observed source usage" className="list-disc pl-5">{report.sources.map((source, index) =>
      <li key={`${source.binding.source.source_id}:${index}`}>{source.phase} · {source.role} · {source.delegated === null ? 'ancestry unknown' : source.delegated ? 'delegated' : 'primary'} · input {tokenValue(source.input_tokens)} · output {tokenValue(source.output_tokens)} · context peak {tokenValue(source.context_peak_tokens)} · compactions {tokenValue(source.compaction_count)} · {source.completeness}</li>)}</ul> : <p>Per-source phase, role and delegation detail: Unknown.</p>}
    {baseline ? <div className="space-y-1 border-t border-slate-300 pt-2">
      <h5 className="font-semibold">Frozen checkpoint reference</h5>
      <p>{baseline.status === 'manual' ? `Manual estimate: ${tokenValue(baseline.reference_tokens)} tokens` : `Learned reference: ${tokenValue(baseline.reference_tokens)} tokens`}</p>
      <p>Mean {tokenValue(baseline.mean_tokens)} · Median {tokenValue(baseline.median_tokens)} · P90 {tokenValue(baseline.p90_tokens)} · {tokenValue(baseline.sample_count)} samples · {tokenValue(baseline.coverage_count)} measured sources</p>
      <p>Warning threshold: {tokenValue(baseline.warning_threshold_tokens)} tokens · Checkpoint threshold: {tokenValue(baseline.checkpoint_threshold_tokens)} tokens · frozen {baseline.frozen_at ?? 'time unknown'} · comparison: {baseline.comparison_scope}</p>
      <p>Reference status: {baseline.status} · digest {baseline.snapshot_digest}</p>
    </div> : <p>Frozen checkpoint reference: Unknown</p>}
  </div>;
}

function routeNames(members: components['schemas']['RouteCohort']['members']) {
  return members.length ? members.map(item => `${item.role}: ${item.provider}/${item.model} (${item.reasoning_effort ?? 'effort unknown'})`).join('; ') : 'Unknown';
}
