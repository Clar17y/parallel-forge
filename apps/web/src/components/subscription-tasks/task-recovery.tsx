'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { ApiError, api, mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';
import { useSessionIdentity } from '@/components/auth/session-identity';
import { bindingStorageKey, matchesStoredBinding, readBinding, removeMatchingBinding, reserveBinding, sameBinding, type RecoveryBinding } from './recovery-binding';

type PreviewRequest = components['schemas']['RecoveryPreviewRequest'];
type Preview = components['schemas']['RecoveryPreview'];
type Receipt = components['schemas']['RecoveryReceipt'];
type Action = PreviewRequest['action'];
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
export const recoveryReason = (code: string) => `${code.replaceAll('_', ' ').replace(/^./, letter => letter.toUpperCase())} (${code})`;
const pathFor = (runId: string, taskId: string, attemptId: string) => `/runs/${runId}/subscription-tasks/${taskId}/attempts/${attemptId}/recovery`;

type Props = {
  runId: string; taskId: string; attemptId: string; taskVersion: number; runVersion: number;
  runAllowsExecution: boolean; taskState: string; pauseRequested: boolean; cancelRequested: boolean;
  unsettledEffects: number; eligibleActions: Action[]; authorityKey: string; onRefresh: () => number;
  onReceipt?: () => void;
  hasRecovery?: boolean;
};

export function TaskRecovery(props: Props) {
  const actorId = useSessionIdentity();
  return <TaskRecoveryInstance key={`${actorId}:${props.runId}:${props.taskId}:${props.attemptId}`} {...props} actorId={actorId} />;
}

function TaskRecoveryInstance({ runId, taskId, attemptId, actorId, taskVersion, runVersion, runAllowsExecution, taskState, pauseRequested, cancelRequested, unsettledEffects, eligibleActions, authorityKey, onRefresh, onReceipt, hasRecovery = true }: Props & { actorId: string | null }) {
  const identity = { actorId: actorId ?? '', runId, taskId, attemptId };
  const identityKey = bindingStorageKey(identity.actorId, runId, taskId, attemptId);
  const reasonId = useId();
  const [preview, setPreview] = useState<{ value: Preview; authorityKey: string } | null>(null);
  const [reason, setReason] = useState('');
  const [binding, setBinding] = useState<RecoveryBinding | null>(() => actorId ? readBinding(identity).binding : null);
  const [storageError, setStorageError] = useState(() => !actorId || readBinding(identity).error);
  const [pending, setPending] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [message, setMessage] = useState('');
  const [receipt, setReceipt] = useState<{ value: Receipt; binding: RecoveryBinding } | null>(null);
  const receiptRef = useRef(receipt);
  receiptRef.current = receipt;
  const [clock, setClock] = useState(Date.now());
  const busy = useRef(false);
  const lifecycle = useRef<AbortController | null>(null);
  const currentIdentity = useRef(identityKey);
  currentIdentity.current = identityKey;
  useEffect(() => {
    const owner = new AbortController(); lifecycle.current = owner;
    const changed = (event: StorageEvent) => {
      if (event.key !== identityKey && event.key !== null) return;
      const next = actorId ? readBinding({ actorId, runId, taskId, attemptId }) : { binding: null, error: true };
      const acknowledged = !!(next.binding && receiptRef.current && sameBinding(receiptRef.current.binding, next.binding));
      setBinding(next.binding); setStorageError(next.error);
      setReceipt(current => current && next.binding && !sameBinding(current.binding, next.binding) ? null : current);
      setPreview(null);
      setMessage(acknowledged || (!next.binding && receiptRef.current) ? '' : next.binding
        ? 'Another tab saved a recovery request for this attempt. Retry that request before starting a new one.'
        : 'Task state changed. Refresh and request a fresh preview before applying recovery.');
    };
    window.addEventListener('storage', changed);
    return () => { owner.abort(); window.removeEventListener('storage', changed); };
  }, [actorId, runId, taskId, attemptId, identityKey]);
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
    if (busy.current || previewing || blocked || !eligibleActions.includes(action) || binding || storageError) return;
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
      if (!next.eligible) setMessage(next.reason_code ? `Recovery unavailable: ${recoveryReason(next.reason_code)}` : 'Recovery is not eligible.');
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
    if (busy.current || pending || blocked || !previewCurrent || !selectedAction || !validReason || binding || storageError || !actorId) {
      if (!validReason && previewCurrent) setMessage('Operator reason is required');
      return;
    }
    const selected: RecoveryBinding = { actorId, runId, taskId, attemptId, key: crypto.randomUUID(), body: { action: selectedAction, preview_token: preview!.value.preview_token, reason: reason.trim() } };
    const owner = lifecycle.current;
    if (!owner) return;
    const reservation = await reserveBinding(selected, owner.signal);
    if (owner.signal.aborted || currentIdentity.current !== identityKey) return;
    if (reservation !== 'saved') {
      const latest = readBinding(selected);
      setBinding(latest.binding); setStorageError(latest.error);
      setMessage(reservation === 'existing' ? 'A recovery request is already saved. Retry that request first.' : 'Recovery request could not be saved or this browser cannot coordinate requests. No recovery was sent.');
      return;
    }
    setBinding(selected);
    await apply(selected);
  }
  async function apply(selected = binding) {
    if (!selected || busy.current || !lifecycle.current || currentIdentity.current !== identityKey) return;
    if (!matchesStoredBinding(selected)) {
      const latest = readBinding(selected);
      setBinding(latest.binding); setStorageError(latest.error);
      setMessage('Saved recovery request changed. Refresh and review it before retrying.');
      return;
    }
    const signal = lifecycle.current.signal;
    busy.current = true; setPending(true); setMessage('');
    try {
      const result = await mutate<Receipt>(pathFor(runId, taskId, attemptId), selected.body, { idempotencyKey: selected.key, signal });
      if (!result || result.run_id !== runId || result.task_id !== taskId || result.attempt_id !== attemptId
        || result.action !== selected.body.action || result.status !== 'applied' || !result.receipt_id) throw new Error('Receipt unavailable');
      if (!signal.aborted && currentIdentity.current === identityKey) {
        const removed = await removeMatchingBinding(selected, signal);
        if (signal.aborted || currentIdentity.current !== identityKey) return;
        const latest = readBinding(selected);
        if (latest.binding && !sameBinding(latest.binding, selected)) return;
        setBinding(latest.binding); setStorageError(latest.error); setReceipt({ value: result, binding: selected }); setPreview(null); setReason('');
        setMessage(removed ? 'Recovery applied. The receipt is authoritative; refreshing task state.' : 'Recovery applied. The receipt is authoritative, but the saved request could not be cleared.'); onReceipt?.(); onRefresh();
      }
    } catch (error) {
      if (signal.aborted || currentIdentity.current !== identityKey) return;
      if (error instanceof ApiError && [409, 422].includes(error.status)) {
        const removed = await removeMatchingBinding(selected, signal);
        if (signal.aborted || currentIdentity.current !== identityKey) return;
        const latest = readBinding(selected);
        if (latest.binding && !sameBinding(latest.binding, selected)) return;
        setBinding(latest.binding); setStorageError(latest.error); setPreview(null);
        setMessage(!removed ? 'The request was rejected, but the saved request could not be cleared. Review it before retrying.' : error.status === 409 ? 'The preview is stale or expired. Review refreshed task state and request a new preview.' : 'The request was rejected. Review the request and refreshed task state before trying again.'); onRefresh();
      } else if (error instanceof ApiError && error.status === 401) {
        setMessage('Sign-in expired. Use a fresh sign-in link and inspect recorded receipts. A new session cannot retry this saved request.');
      } else if (error instanceof ApiError && error.status === 403) {
        setMessage('The request was not authorized. The saved request remains pending; inspect recorded receipts after restoring access.');
      } else setMessage('The result could not be confirmed. Retry the same request to retrieve its durable receipt.');
    } finally { busy.current = false; if (!signal.aborted) setPending(false); }
  }

  if (!hasRecovery && !binding && !storageError && !receipt) return null;
  return <section aria-label="Task recovery" className="space-y-2 border-t border-slate-300 pt-3">
    <h4 className="font-semibold">Recovery</h4>
    <p>Recovery can retry a saved result or correct the task’s approved instructions. A completed tool entry is past activity and does not mean a tool is still running.</p>
    {blocked ? <p>Recovery is unavailable while work is active, a stop is requested, or the run cannot continue. Refresh to check the current state.</p> : null}
    {!eligibleActions.length ? <p>No recovery action is available for the current task state.</p> : eligibleActions.map(action => <div key={action} className="space-y-1">
      <p>{descriptions[action]}</p><button type="button" disabled={blocked || previewing || pending || !!binding || storageError} onClick={() => void requestPreview(action)}>{previewing ? 'Loading recovery preview…' : labels[action]}</button>
    </div>)}
    {preview ? <div aria-label="Recovery preview" className="space-y-2 rounded border border-slate-300 p-3 [overflow-wrap:anywhere]">
      <h5 className="font-semibold">Preview · {labels[preview.value.action]}</h5>
      {!preview.value.eligible ? <p>{preview.value.message} {recoveryReason(preview.value.reason_code)}</p> : <>
        <p>{preview.value.message}</p>
        <p>Expires {preview.value.expires_at}</p><p>Changes: {preview.value.changes.join('; ') || 'None reported'}</p>
        <p>Retained evidence: {preview.value.retained_evidence.join('; ') || 'None reported'}</p>
        <p>Provider attempts: {preview.value.budget_impact.provider_attempts} · Repair units: {preview.value.budget_impact.repair_units}</p>
        <label htmlFor={reasonId}>Recovery reason</label>
        <textarea id={reasonId} rows={2} maxLength={512} value={reason} readOnly={!!binding || pending}
          onChange={event => setReason(event.target.value)} className="w-full rounded border border-slate-400 p-2" />
        <button type="button" disabled={!previewCurrent || blocked || pending || !!binding || storageError} onClick={() => void submit()}>Apply recovery</button>
        {!previewCurrent ? <p>This preview is expired or no longer matches current state. Request a fresh preview.</p> : null}
      </>}
    </div> : null}
    {binding && (!receipt || !sameBinding(binding, receipt.binding)) ? <div className="space-y-2 rounded border border-amber-500 p-3"><p>A recovery request is saved for this attempt. Its outcome is unconfirmed. Retry the same request to retrieve its receipt; this will not request a new recovery action.</p>
      <button type="button" disabled={pending} onClick={() => void apply()}>Retry same request</button></div> : null}
    {storageError ? <p role="alert">Saved recovery requests are unavailable or unreadable in this browser. Recovery is disabled until browser storage is available.</p> : null}
    {pending ? <p role="status">Applying recovery…</p> : null}
    {receipt ? <div role="status"><p>Recovery applied. Recovery receipt {receipt.value.receipt_id}</p><p>Action: {labels[receipt.value.action]}</p><p>Status: {receipt.value.status}</p><p>Recorded: {receipt.value.observed_at}</p><p>Reason: {recoveryReason(receipt.value.reason_code)}</p></div> : null}
    {message ? <p role="status">{message}</p> : null}
  </section>;
}
