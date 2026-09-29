'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { ApiError, api, mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';

type PreviewRequest = components['schemas']['RecoveryPreviewRequest'];
type Preview = components['schemas']['RecoveryPreview'];
type ApplyRequest = components['schemas']['RecoveryApplyRequest'];
type Receipt = components['schemas']['RecoveryReceipt'];
type Action = PreviewRequest['action'];
type Binding = { key: string; body: ApplyRequest };
const labels: Record<Action, string> = {
  retry_application: 'Retry saved result',
  reject_and_retry_step: 'Retry step',
  repair_approved_plan_contract: 'Repair approved-plan instructions',
};
const descriptions: Record<Action, string> = {
  retry_application: 'Reprocess the saved result without starting another provider attempt.',
  reject_and_retry_step: 'Retain the failed result and queue one fresh attempt after stop evidence is confirmed.',
  repair_approved_plan_contract: 'Replace the approved planning-only instructions with the approved implementation contract, then queue a bounded attempt.',
};
const maxReasonBytes = 512;
const humanizeCode = (code: string) => `${code.replaceAll('_', ' ').replace(/^./, letter => letter.toUpperCase())} (${code})`;
const pathFor = (runId: string, taskId: string, attemptId: string) => `/runs/${runId}/subscription-tasks/${taskId}/attempts/${attemptId}/recovery`;

export function TaskRecovery({ runId, taskId, attemptId, taskVersion, runVersion, runAllowsExecution, taskState, pauseRequested, cancelRequested, unsettledEffects, eligibleActions, authorityKey, onRefresh }: {
  runId: string; taskId: string; attemptId: string; taskVersion: number; runVersion: number;
  runAllowsExecution: boolean; taskState: string; pauseRequested: boolean; cancelRequested: boolean;
  unsettledEffects: number; eligibleActions: Action[]; authorityKey: string; onRefresh: () => number;
}) {
  const reasonId = useId();
  const [preview, setPreview] = useState<{ value: Preview; authorityKey: string } | null>(null);
  const [reason, setReason] = useState('');
  const [binding, setBinding] = useState<Binding | null>(null);
  const [pending, setPending] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [message, setMessage] = useState('');
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [clock, setClock] = useState(Date.now());
  const busy = useRef(false);
  const lifecycle = useRef<AbortController | null>(null);
  useEffect(() => { const owner = new AbortController(); lifecycle.current = owner; return () => owner.abort(); }, []);
  useEffect(() => {
    if (!preview) return;
    const delay = Date.parse(preview.value.expires_at) - Date.now();
    if (delay <= 0) return;
    const timer = setTimeout(() => setClock(Date.now()), delay + 1);
    return () => clearTimeout(timer);
  }, [preview]);

  const selectedAction = preview?.value.action;
  const previewCurrent = Boolean(preview && preview.value.eligible && selectedAction && eligibleActions.includes(selectedAction)
    && preview.value.run_id === runId && preview.value.task_id === taskId && preview.value.attempt_id === attemptId
    && Date.parse(preview.value.expires_at) > clock && preview.authorityKey === authorityKey);
  const validReason = reason.trim().length > 0 && new TextEncoder().encode(reason).length <= maxReasonBytes;
  const active = ['leased', 'running', 'executing'].includes(taskState.toLowerCase());
  const blocked = active || unsettledEffects > 0 || pauseRequested || cancelRequested || !runAllowsExecution
    || !Number.isSafeInteger(taskVersion) || taskVersion < 0 || !Number.isSafeInteger(runVersion) || runVersion < 0;

  async function requestPreview(action: Action) {
    if (busy.current || previewing || blocked || !eligibleActions.includes(action) || binding) return;
    const signal = lifecycle.current?.signal;
    if (!signal || signal.aborted) return;
    setPreviewing(true); setMessage(''); setPreview(null); setReceipt(null);
    try {
      const next = await api<Preview>(`${pathFor(runId, taskId, attemptId)}/preview`, {
        method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action } satisfies PreviewRequest),
      });
      if (signal.aborted) return;
      if (!next || next.run_id !== runId || next.task_id !== taskId || next.attempt_id !== attemptId || next.action !== action) throw new Error('Preview unavailable');
      setClock(Date.now());
      setPreview({ value: next, authorityKey });
      if (!next.eligible) setMessage(next.reason_code ? `Recovery unavailable: ${humanizeCode(next.reason_code)}` : 'Recovery is not eligible.');
    } catch (error) {
      if (!signal.aborted) { setMessage(error instanceof ApiError && error.status === 409 ? 'Task, run, or attempt changed. Refresh state before previewing again.' : 'Recovery preview unavailable. Refresh task state and try again.'); onRefresh(); }
    } finally { if (!signal.aborted) setPreviewing(false); }
  }

  async function submit() {
    if (preview && Date.parse(preview.value.expires_at) <= Date.now()) {
      setClock(Date.now());
      setMessage('This preview has expired. Request a fresh preview before applying recovery.');
      return;
    }
    if (busy.current || pending || blocked || !previewCurrent || !selectedAction || !validReason || binding) {
      if (!validReason && previewCurrent) setMessage('Operator reason is required');
      return;
    }
    const selected: Binding = { key: crypto.randomUUID(), body: { action: selectedAction, preview_token: preview!.value.preview_token, reason: reason.trim() } };
    await apply(selected);
  }
  async function apply(selected = binding) {
    if (!selected || busy.current || !lifecycle.current) return;
    const signal = lifecycle.current.signal;
    busy.current = true; setPending(true); setBinding(selected); setMessage('');
    try {
      const result = await mutate<Receipt>(pathFor(runId, taskId, attemptId), selected.body, { idempotencyKey: selected.key, signal });
      if (!result || result.run_id !== runId || result.task_id !== taskId || result.attempt_id !== attemptId
        || result.action !== selected.body.action || result.status !== 'applied' || !result.receipt_id) throw new Error('Receipt unavailable');
      if (!signal.aborted) { setReceipt(result); setBinding(null); setPreview(null); setReason(''); setMessage('Recovery applied. The receipt is authoritative; refreshing task state.'); onRefresh(); }
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof ApiError && error.status === 409) {
        setBinding(null); setPreview(null); setMessage('The preview is stale or expired. Review refreshed task state and request a new preview.'); onRefresh();
      } else if (error instanceof ApiError && [401, 403, 422].includes(error.status)) {
        setBinding(null); setMessage('The request was rejected. Check your operator session and request before trying again.');
      } else setMessage('The result could not be confirmed. Retry the same request to retrieve its durable receipt.');
    } finally { busy.current = false; if (!signal.aborted) setPending(false); }
  }

  return <section aria-label="Task recovery" className="space-y-2 border-t border-slate-300 pt-3">
    <h4 className="font-semibold">Recovery</h4>
    <p>Recovery can retry a saved result or correct the task’s approved instructions. A completed tool entry is past activity and does not mean a tool is still running.</p>
    {blocked ? <p>Recovery is unavailable while work is active, a stop is requested, or the run cannot continue. Refresh to check the current state.</p> : null}
    {!eligibleActions.length ? <p>No recovery action is available for the current task state.</p> : eligibleActions.map(action => <div key={action} className="space-y-1">
      <p>{descriptions[action]}</p><button type="button" disabled={blocked || previewing || pending || !!binding} onClick={() => void requestPreview(action)}>{previewing ? 'Loading recovery preview…' : labels[action]}</button>
    </div>)}
    {preview ? <div aria-label="Recovery preview" className="space-y-2 rounded border border-slate-300 p-3 [overflow-wrap:anywhere]">
      <h5 className="font-semibold">Preview · {labels[preview.value.action]}</h5>
      {!preview.value.eligible ? <p>{preview.value.message} {humanizeCode(preview.value.reason_code)}</p> : <>
        <p>{preview.value.message}</p>
        <p>Expires {preview.value.expires_at}</p><p>Changes: {preview.value.changes.join('; ') || 'None reported'}</p>
        <p>Retained evidence: {preview.value.retained_evidence.join('; ') || 'None reported'}</p>
        <p>Provider attempts: {preview.value.budget_impact.provider_attempts} · Repair units: {preview.value.budget_impact.repair_units}</p>
        <label htmlFor={reasonId}>Recovery reason</label>
        <textarea id={reasonId} rows={2} maxLength={512} value={reason} readOnly={!!binding || pending}
          onChange={event => setReason(event.target.value)} className="w-full rounded border border-slate-400 p-2" />
        <button type="button" disabled={!previewCurrent || blocked || pending || !!binding} onClick={() => void submit()}>Apply recovery</button>
        {!previewCurrent ? <p>This preview is expired or no longer matches current state. Request a fresh preview.</p> : null}
      </>}
    </div> : null}
    {binding ? <div className="space-y-2 rounded border border-amber-500 p-3"><p>The outcome is unconfirmed. Retry this exact recovery request with the same idempotency key.</p>
      <button type="button" disabled={pending} onClick={() => void apply()}>Retry same request</button></div> : null}
    {pending ? <p role="status">Applying recovery…</p> : null}
    {receipt ? <div role="status"><p>Recovery applied. Recovery receipt {receipt.receipt_id}</p><p>Action: {labels[receipt.action]}</p><p>Status: {receipt.status}</p><p>Recorded: {receipt.observed_at}</p><p>Reason: {humanizeCode(receipt.reason_code)}</p></div> : null}
    {message ? <p role="status">{message}</p> : null}
  </section>;
}
