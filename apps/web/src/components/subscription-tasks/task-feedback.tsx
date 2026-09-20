'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { ApiError, mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';

type Task = components['schemas']['SubscriptionTaskView'];
type Request = components['schemas']['TaskFeedbackRequest'];
type Receipt = components['schemas']['TaskFeedbackReceipt'];
type FeedbackView = components['schemas']['SubscriptionTaskFeedbackView'];
type Binding = { key: string; body: Request };

const maxFeedbackBytes = 4096;
const statusLabels: Record<FeedbackView['status'], string> = {
  pending_primary: 'Pending primary forwarding',
  forwarded: 'Forwarded to worker',
  delivered: 'Delivered to worker',
  closed: 'Closed',
};
const closedReasonLabels: Record<NonNullable<FeedbackView['closed_reason']>, string> = {
  accepted: 'task already accepted',
  cancelled: 'task cancelled',
  budget_exhausted: 'task budget exhausted',
};

export function TaskFeedback({ runId, runVersion, runIsTerminal = false, task, projectionToken, onRefresh }: {
  runId: string;
  runVersion: number;
  runIsTerminal?: boolean;
  task: Task;
  projectionToken: number;
  onRefresh: () => number;
}) {
  const feedbackId = useId();
  const [feedback, setFeedback] = useState('');
  const [binding, setBinding] = useState<Binding | null>(null);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState('');
  const [fenceToken, setFenceToken] = useState<number | null>(null);
  const busy = useRef(false);
  const lifecycle = useRef<AbortController | null>(null);
  useEffect(() => {
    const owner = new AbortController();
    lifecycle.current = owner;
    return () => owner.abort();
  }, []);

  if (task.purpose === 'primary' || !task.parent_task_id) return null;

  const fenced = fenceToken !== null && projectionToken < fenceToken;
  const feedbackBytes = new TextEncoder().encode(feedback).length;
  const validFeedback = feedback.trim().length > 0
    && !feedback.includes('\0')
    && feedbackBytes <= maxFeedbackBytes;
  const canSubmit = !runIsTerminal
    && !task.cancel_requested
    && Number.isSafeInteger(runVersion)
    && runVersion >= 0
    && Number.isSafeInteger(task.version)
    && task.version >= 0;
  const receipts = task.feedback_receipts ?? [];

  async function submit() {
    if (busy.current || fenced || !lifecycle.current || (!binding && (!canSubmit || !validFeedback))) return;
    const selected = binding ?? {
      key: crypto.randomUUID(),
      body: {
        expected_run_version: runVersion,
        expected_task_version: task.version,
        feedback,
      },
    };
    const signal = lifecycle.current.signal;
    busy.current = true;
    setPending(true);
    setBinding(selected);
    setMessage('');
    try {
      const result = await mutate<Receipt>(
        `/runs/${runId}/subscription-tasks/${task.task_id}/feedback`,
        selected.body,
        { idempotencyKey: selected.key, signal },
      );
      const selectedBytes = new TextEncoder().encode(selected.body.feedback).length;
      if (!result
        || result.run_id !== runId
        || result.task_id !== task.task_id
        || result.primary_task_id !== task.parent_task_id
        || result.run_version !== selected.body.expected_run_version
        || result.task_version !== selected.body.expected_task_version
        || result.feedback_bytes !== selectedBytes
        || !/^[0-9a-f]{64}$/.test(result.feedback_digest)
        || !/^[0-9a-f]{64}$/.test(result.binding_digest)
        || !Object.hasOwn(statusLabels, result.status)) {
        throw new Error('Feedback receipt unavailable');
      }
      if (!signal.aborted) {
        setBinding(null);
        setFeedback('');
        setMessage('Feedback recorded. The primary coordinator will forward its retained receipt.');
        setFenceToken(onRefresh());
      }
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof ApiError && error.status === 409) {
        setBinding(null);
        setMessage('This feedback was not accepted against the current task state. Review the refreshed receipts before sending it again.');
        setFenceToken(onRefresh());
      } else if (error instanceof ApiError && [401, 403, 422].includes(error.status)) {
        setBinding(null);
        setMessage('The feedback was rejected. Check your operator session and text before trying again.');
      } else {
        setMessage('The feedback receipt could not be confirmed. Retry the same request.');
      }
    } finally {
      busy.current = false;
      if (!signal.aborted) setPending(false);
    }
  }

  return <section aria-label="Worker feedback" className="space-y-2 border-t border-slate-300 pt-3">
    <h3 className="font-semibold">Worker feedback</h3>
    <p>Target worker: {task.purpose} task {task.task_id}</p>
    <p className="text-sm">The primary coordinator forwards feedback; this does not issue provider commands or change task authority.</p>
    {receipts.length > 0 ? <ul aria-label="Retained feedback receipts" className="space-y-2">
      {receipts.map(receipt => <li key={receipt.receipt_id} className="rounded bg-[var(--surface-muted)] p-2 text-sm [overflow-wrap:anywhere]">
        <p>{statusLabels[receipt.status]}{receipt.closed_reason ? ` · ${closedReasonLabels[receipt.closed_reason]}` : ''}</p>
        <p>Receipt {receipt.receipt_id}</p>
        <p>{receipt.feedback_bytes.toLocaleString()} bytes · digest {receipt.feedback_digest}</p>
        <p>Recorded <time dateTime={receipt.observed_at}>{receipt.observed_at}</time></p>
      </li>)}
    </ul> : <p>No retained worker feedback receipts.</p>}
    {canSubmit || binding ? <>
      <label htmlFor={feedbackId} className="block">Feedback for {task.purpose} worker</label>
      <textarea id={feedbackId} rows={4} maxLength={maxFeedbackBytes} value={feedback} readOnly={!!binding || fenced}
        onChange={event => setFeedback(event.target.value)} className="w-full rounded border border-slate-400 p-2" />
      <p className="text-sm">{feedbackBytes.toLocaleString()} / {maxFeedbackBytes.toLocaleString()} UTF-8 bytes</p>
      {binding
        ? <button type="button" disabled={pending || fenced} onClick={() => void submit()}>Retry same request</button>
        : <button type="button" disabled={pending || fenced || !validFeedback} onClick={() => void submit()}>Send worker feedback</button>}
    </> : <p>New feedback is unavailable for this task state.</p>}
    {pending ? <p role="status">Recording worker feedback…</p> : null}
    {message ? <p role="status">{message}</p> : null}
  </section>;
}
