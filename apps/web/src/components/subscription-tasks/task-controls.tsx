'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { ApiError, mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';

type Task = components['schemas']['SubscriptionTaskView'];
type Request = components['schemas']['TaskControlRequest'];
type Receipt = components['schemas']['TaskControlReceipt'];
type Action = Request['action'];
type Binding = { key: string; body: Request };
const statuses: Record<string, string> = {
  pause_requested: 'Pause requested', cancel_requested: 'Cancellation requested',
  paused: 'Paused', cancelled: 'Cancelled', queued: 'Resume recorded', blocked: 'Waiting for a decision', decision_pending: 'Resume recorded',
};
const messages: Record<Receipt['status'], string> = {
  pause_requested: 'Pause requested. Waiting for stopped work to be confirmed.',
  cancel_requested: 'Cancellation requested. Waiting for stopped work to be confirmed.',
  paused: 'Task paused.', cancelled: 'Task cancelled.',
  queued: 'Resume recorded. The scheduler will recheck eligibility before launching work.',
  blocked: 'Resume recorded. The task is still waiting for its pending decision.',
  decision_pending: 'Resume recorded. The retained decision is ready for guarded application.',
};

export function TaskControlSummary({ task }: { task: Task }) {
  return task.control ? <>
    <p>{statuses[task.control.status]}</p><p>Reason: {task.control.reason}</p>
    <p className="text-sm">Reported {task.control.observed_at}</p>
  </> : task.pause_requested || task.cancel_requested ? <p>Stop requested; settled control evidence is not available in this snapshot.</p> : null;
}

export function TaskControls({ runId, runVersion, runAllowsExecution, runIsTerminal = false, task, projectionToken, onRefresh }: {
  runId: string; runVersion: number; runAllowsExecution: boolean; runIsTerminal?: boolean; task: Task; projectionToken: number; onRefresh: () => number;
}) {
  const reasonId = useId();
  const [reason, setReason] = useState('');
  const [binding, setBinding] = useState<Binding | null>(null);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState('');
  const [fenceToken, setFenceToken] = useState<number | null>(null);
  const busy = useRef(false);
  const lifecycle = useRef<AbortController | null>(null);
  useEffect(() => { const owner = new AbortController(); lifecycle.current = owner; return () => owner.abort(); }, []);

  const fenced = fenceToken !== null && projectionToken < fenceToken;
  const validReason = reason.trim().length > 0 && new TextEncoder().encode(reason).length <= 512;
  const stopped = ['terminal', 'cancelled', 'completed', 'failed'].includes(task.state) || task.cancel_requested;
  const actions: Action[] = [];
  if (task.purpose !== 'primary' && !stopped && !runIsTerminal && Number.isSafeInteger(runVersion) && runVersion >= 0) {
    if (runAllowsExecution && !task.pause_requested) actions.push('pause');
    if (runAllowsExecution && task.pause_requested && task.control?.status === 'paused' && task.control.pause_receipt_id) actions.push('resume');
    actions.push('cancel');
  }

  async function submit(action?: Action) {
    if (busy.current || fenced || !lifecycle.current || (!binding && (!action || !actions.includes(action) || !validReason))) return;
    const selected = binding ?? { key: crypto.randomUUID(), body: {
      action: action!, expected_run_version: runVersion, expected_task_version: task.version,
      reason, pause_receipt_id: action === 'resume' ? task.control!.pause_receipt_id : null,
    } };
    const signal = lifecycle.current.signal;
    busy.current = true; setPending(true); setBinding(selected); setMessage('');
    try {
      const result = await mutate<Receipt>(`/runs/${runId}/subscription-tasks/${task.task_id}/controls`, selected.body,
        { idempotencyKey: selected.key, signal });
      if (!result || result.run_id !== runId || result.task_id !== task.task_id || result.action !== selected.body.action
        || result.run_version !== selected.body.expected_run_version || !Object.hasOwn(messages, result.status)
        || (result.pause_receipt_id ?? null) !== (selected.body.pause_receipt_id ?? null)) throw new Error('Receipt unavailable');
      if (!signal.aborted) {
        setBinding(null);
        setReason('');
        setMessage(messages[result.status]);
        setFenceToken(onRefresh());
      }
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof ApiError && error.status === 409) {
        setBinding(null);
        setReason('');
        setMessage('The task or run changed. Review the refreshed state before acting again.');
        setFenceToken(onRefresh());
      } else if (error instanceof ApiError && [401, 403, 422].includes(error.status)) {
        setBinding(null);
        setMessage('The request was rejected. Check your operator session and request before trying again.');
      } else {
        setMessage('The request could not be confirmed. Retry the same request to retrieve its durable receipt.');
      }
    } finally { busy.current = false; if (!signal.aborted) setPending(false); }
  }

  return <section aria-label="Task controls" className="space-y-2 border-t border-slate-300 pt-3">
    <TaskControlSummary task={task} />
    {task.purpose === 'primary' ? <p>Use run controls for the primary coordinator.</p> : null}
    {actions.length > 0 || binding ? <>
      <label htmlFor={reasonId} className="block">Task control reason</label>
      <textarea id={reasonId} rows={2} maxLength={512} value={reason} readOnly={!!binding || fenced}
        onChange={event => setReason(event.target.value)} className="w-full rounded border border-slate-400 p-2" />
      <div className="flex flex-wrap gap-3">{binding ?
        <button type="button" disabled={pending || fenced} onClick={() => void submit()}>Retry same request</button> : <>
          {actions.includes('pause') ? <button type="button" disabled={pending || fenced || !validReason} onClick={() => void submit('pause')}>Pause task</button> : null}
          {actions.includes('resume') ? <button type="button" disabled={pending || fenced || !validReason} onClick={() => void submit('resume')}>Resume task</button> : null}
          {actions.includes('cancel') ? <button type="button" disabled={pending || fenced || !validReason} onClick={() => void submit('cancel')}>Cancel task</button> : null}
        </>}
      </div>
    </> : null}
    {pending ? <p role="status">Recording task control…</p> : null}
    {message ? <p role="status">{message}</p> : null}
  </section>;
}
