'use client';
import { useEffect, useState } from 'react';
import { useParams } from 'next/navigation';
import { LoadingStatus } from '@/components/ui/loading-status';
import { useApi } from '@/hooks/use-api';
import { mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';

type Run = components['schemas']['DirectRunResponse'];
type ChildTask = components['schemas']['DirectTaskResponse'];
type TaskDraft = { id?: string; task: string; shape: 'quick' | 'planned' };
const TERMINAL = new Set(['DONE', 'FAILED', 'CANCELLED', 'NEEDS_DECISION', 'AWAITING_PLAN_APPROVAL', 'AWAITING_DECOMPOSITION_APPROVAL']);

export default function DirectDetailPage() {
  const { id } = useParams<{ id: string }>();
  const run = useApi<Run>(`/direct-runs/${encodeURIComponent(id)}`, {
    keepPreviousOnRefresh: true, keepPreviousOnError: true,
  });
  const terminal = run.value ? TERMINAL.has(run.value.state) : false;
  const refreshRun = run.refresh;
  useEffect(() => {
    if (!run.value || terminal) return;
    const timer = setTimeout(refreshRun, 3000);
    return () => clearTimeout(timer);
  }, [run.value, terminal, refreshRun]);
  const transcript = useApi<components['schemas']['DirectTranscriptResponse']>(
    `/direct-runs/${encodeURIComponent(id)}/transcript`,
    { refreshIntervalMs: terminal ? undefined : 3000 },
  );
  const diff = useApi<components['schemas']['DirectDiffResponse']>(
    terminal ? `/direct-runs/${encodeURIComponent(id)}/diff` : null,
  );
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [draftVersion, setDraftVersion] = useState<number | null>(null);
  const [draft, setDraft] = useState<TaskDraft[] | null>(null);
  async function cancel() {
    try {
      await mutate(`/direct-runs/${encodeURIComponent(id)}/cancel`, {}, { idempotencyKey: crypto.randomUUID() });
      run.refresh();
    } catch { setError('Cancel failed.'); }
  }
  async function approvePlan() {
    setBusy(true); setError('');
    try {
      await mutate(`/direct-runs/${encodeURIComponent(id)}/approve-plan`, { expected_version: run.value?.control_version, task_id: run.value?.approval_task_id }, { idempotencyKey: crypto.randomUUID() });
      run.refresh();
    } catch { setError('Plan approval failed. Refresh the run before retrying.'); }
    finally { setBusy(false); }
  }
  async function approveDecomposition() {
    setBusy(true); setError('');
    try {
      await mutate(`/direct-runs/${encodeURIComponent(id)}/approve-decomposition`, { expected_version: run.value?.control_version }, { idempotencyKey: crypto.randomUUID() });
      setDraft(null); setDraftVersion(null);
      run.refresh();
    } catch { setError('Decomposition approval failed. Refresh the run before retrying.'); }
    finally { setBusy(false); }
  }
  async function saveTasks(tasks: TaskDraft[], expectedVersion = draftVersion ?? run.value?.control_version) {
    setBusy(true); setError('');
    try {
      await mutate(`/direct-runs/${encodeURIComponent(id)}/tasks`, { tasks, expected_version: expectedVersion }, { idempotencyKey: crypto.randomUUID(), method: 'PUT' });
      setDraft(null); setDraftVersion(null);
      run.refresh();
    } catch { setError('Task update failed. Your edits are preserved below. Refresh to inspect the current run.'); }
    finally { setBusy(false); }
  }
  if (run.loading) return <LoadingStatus>Loading direct run…</LoadingStatus>;
  if (!run.value) return <><p role="alert">Direct run unavailable.</p><button onClick={refreshRun}>Refresh run</button></>;
  const value = run.value;
  const parked = value.state === 'AWAITING_DECOMPOSITION_APPROVAL';
  const source: ChildTask[] = [...(value.tasks ?? [])].sort((a, b) => a.ordinal - b.ordinal);
  const effective: TaskDraft[] = draft ?? source.map(t => ({ id: t.id, task: t.task, shape: t.shape === 'planned' ? 'planned' : 'quick' }));
  const staleDraft = draft !== null && draftVersion !== value.control_version;
  const currentId = source.find(t => ['PLANNING', 'RUNNING', 'CHECKING', 'REVIEWING', 'REPAIRING', 'AWAITING_PLAN_APPROVAL', 'NEEDS_DECISION'].includes(t.state))?.id;
  function updateDraft(next: TaskDraft[]) {
    setDraftVersion(draftVersion ?? value.control_version ?? 0);
    setDraft(next);
  }
  function moveDraft(index: number, delta: -1 | 1) {
    const next = [...effective];
    const [item] = next.splice(index, 1);
    next.splice(index + delta, 0, item);
    updateDraft(next);
  }
  return <>
    <h1>Direct run</h1>
    <button type="button" disabled={busy || run.refreshing} onClick={refreshRun}>Refresh run</button>
    {run.failed && <p role="alert">Refresh failed. The last loaded snapshot and your edits are preserved. Refresh again before applying edits.</p>}
    <p>State: {value.state}</p>
    <p>{value.task}</p>
    {value.summary && <p>{value.summary}</p>}
    {value.plan_text && <section><h2>Plan</h2><pre>{value.plan_text}</pre>{value.state === 'AWAITING_PLAN_APPROVAL' && <button disabled={busy} onClick={approvePlan}>Approve plan and continue</button>}</section>}
    {value.state === 'NEEDS_DECISION' && value.question && <section><h2>Decision needed</h2><p>{value.question}</p><ul>{value.options.map(o => <li key={o}>{o}</li>)}</ul></section>}
    {value.failure_reason && <p role="alert">{value.failure_reason}</p>}
    {(value.pr_url || value.pr_number) && <section><h2>Pull request</h2>{value.pr_url ? <p><a href={value.pr_url} target="_blank" rel="noreferrer">{value.pr_number ? `PR #${value.pr_number}` : 'Pull request'}</a></p> : <p>PR #{value.pr_number}</p>}<p>PR rounds: {value.pr_rounds ?? 0}</p></section>}
    {(!terminal || value.state === 'AWAITING_PLAN_APPROVAL' || value.state === 'AWAITING_DECOMPOSITION_APPROVAL') && <button onClick={cancel}>Cancel run</button>}
    {error && <p role="alert">{error}</p>}
    {draft !== null && <section>
      <h2>Retained task edits</h2>
      {value.state === 'AWAITING_DECOMPOSITION_APPROVAL'
        ? <><p>You can copy your retained edits before discarding them and using the latest saved task list.</p>
          <button type="button" disabled={busy || run.refreshing || run.failed} onClick={() => {
            setDraft(null); setDraftVersion(null); setError('');
          }}>Use current task list</button></>
        : <p>The run is now {value.state}. Your edits are retained for copying; they cannot be applied to this run.</p>}
      <textarea aria-label="Retained task edits" readOnly rows={10} value={JSON.stringify(draft, null, 2)} />
    </section>}
    {(value.tasks ?? []).length > 0 && <section><h2>Epic tasks</h2>
      {!parked ? <ol>{source.map(t => <li key={t.id}>
            <span>{t.ordinal + 1}. [{t.shape}] {t.task} — {t.state}{t.id === currentId ? ' (current)' : ''}</span>
            {t.summary && <p>{t.summary}</p>}
            {t.plan_text && <pre>{t.plan_text}</pre>}
            {value.state === 'AWAITING_PLAN_APPROVAL' && value.approval_task_id === t.id && <button disabled={busy} onClick={approvePlan}>Approve task plan and continue</button>}
            <ul>{(value.checks ?? []).filter(c => c.task_id === t.id).map(c => <li key={c.id}>{c.command_name}: {c.status}</li>)}</ul>
            {t.head_sha && <p>Head: {t.head_sha}</p>}
          </li>)}</ol> : <>
          {staleDraft && <section><h3>Latest saved tasks (snapshot {value.control_version})</h3>
            <ol>{source.map(t => <li key={t.id}>[{t.shape}] {t.task}</li>)}</ol>
            <p>Your retained edits below started from snapshot {draftVersion}. Applying them replaces the saved task list shown above.</p>
            <button type="button" disabled={busy || run.refreshing || run.failed} onClick={() => saveTasks(effective, value.control_version)}>Apply retained edits to snapshot {value.control_version}</button>
          </section>}
          <ol>{effective.map((t, index) => <li key={t.id ?? `new-${index}`}>
            <label>Task {index + 1} <textarea disabled={busy} aria-label={`Task ${index + 1} text`} value={t.task} onChange={e => {
              updateDraft(effective.map((item, i) => i === index ? { ...item, task: e.target.value } : item));
            }} /></label>
            <label>Task {index + 1} shape <select disabled={busy} aria-label={`Task ${index + 1} shape`} value={t.shape} onChange={e => {
              updateDraft(effective.map((item, i) => i === index ? { ...item, shape: e.target.value as TaskDraft['shape'] } : item));
            }}><option value="quick">quick</option><option value="planned">planned</option></select></label>
            <button type="button" disabled={busy || index === 0} onClick={() => moveDraft(index, -1)}>Move task {index + 1} up</button>
            <button type="button" disabled={busy || index === effective.length - 1} onClick={() => moveDraft(index, 1)}>Move task {index + 1} down</button>
          </li>)}</ol>
          <button type="button" disabled={busy || run.refreshing || run.failed || staleDraft} onClick={() => saveTasks(effective)}>Save task order</button>
          <button type="button" disabled={busy || draft !== null} onClick={approveDecomposition}>Approve decomposition and continue</button>
        </>}
    </section>}
    {(value.owner_actions ?? []).length > 0 && <section><h2>Owner actions</h2><ul>{value.owner_actions?.map((action, index) => <li key={index}>{String(action.action)} by {String(action.actor_id ?? 'owner')} at snapshot {String(action.snapshot_version)}{Array.isArray(action.warnings) && action.warnings.length > 0 ? `: ${action.warnings.join(', ')}` : ''}</li>)}</ul></section>}
    <h2>Checks</h2>
    {(value.checks ?? []).length === 0 && <p>No checks recorded yet.</p>}
    <ul>{(value.checks ?? []).map(check => (
      <li key={check.id}>
        <span>Round {check.round}: {check.command_name} ({check.kind}) — {check.status}</span>
        {check.failure_excerpt && <pre>{check.failure_excerpt}</pre>}
      </li>
    ))}</ul>
    <h2>Reviews</h2>
    {(value.reviews ?? []).length === 0 && <p>No reviews recorded yet.</p>}
    <ul>{(value.reviews ?? []).map(review => (
      <li key={review.id}>
        <span>Round {review.round}: {review.client}/{review.model} — {review.verdict}</span>
        {review.summary && <p>{review.summary}</p>}
        {review.reviewer_modified_worktree && <p>Reviewer modified worktree (reverted)</p>}
        <ul>{(review.findings ?? []).map((finding, i) => (
          <li key={i}>[{finding.severity}]{finding.file ? ` ${finding.file}:` : ''} {finding.detail}</li>
        ))}</ul>
      </li>
    ))}</ul>
    <h2>Transcript</h2>
    <pre>{transcript.value?.text ?? ''}</pre>
    {terminal && <><h2>Diff</h2><pre>{diff.value?.diff ?? ''}</pre></>}
  </>;
}
