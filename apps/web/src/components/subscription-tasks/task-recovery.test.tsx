import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { TaskRecovery } from './task-recovery';
import { SessionIdentity } from '@/components/auth/session-identity';
import type { ReactElement } from 'react';
import { bindingStorageKey, removeMatchingBinding, reserveBinding } from './recovery-binding';
import { RecoveryReceiptHistory } from './recovery-receipt-history';

const renderRecovery = (element: ReactElement, actorId = 'actor-1') => render(<SessionIdentity.Provider value={actorId}>{element}</SessionIdentity.Provider>);
const recoveryProps = { runId: 'run-1', taskId: 'task-1', attemptId: 'attempt-1', taskVersion: 2, runVersion: 3, runAllowsExecution: false, taskState: 'running', pauseRequested: false, cancelRequested: false, unsettledEffects: 1, eligibleActions: [] as Array<'retry_application'>, authorityKey: 'changed', onRefresh: vi.fn() };
const stored = (actorId = 'actor-1', runId = 'run-1', taskId = 'task-1', attemptId = 'attempt-1') => ({ actorId, runId, taskId, attemptId, key: '12345678-1234-1234-1234-123456789abc', body: { action: 'retry_application' as const, preview_token: 'expired-preview', reason: 'Retained result' } });

beforeEach(() => Object.defineProperty(navigator, 'locks', { configurable: true, value: { request: async (_name: string, optionsOrCallback: { signal?: AbortSignal } | (() => unknown), maybeCallback?: () => unknown) => (typeof optionsOrCallback === 'function' ? optionsOrCallback : maybeCallback!)() } }));
afterEach(() => { cleanup(); localStorage.clear(); Object.defineProperty(navigator, 'locks', { configurable: true, value: undefined }); vi.restoreAllMocks(); });

test('two tabs reserving the same attempt cannot send two distinct requests', async () => {
  let tail = Promise.resolve();
  Object.defineProperty(navigator, 'locks', { configurable: true, value: { request: (_name: string, _options: object, callback: () => unknown) => {
    const next = tail.then(callback);
    tail = next.then(() => undefined);
    return next;
  } } });
  const a = stored();
  const b = { ...a, key: '99999999-1234-1234-1234-123456789abc' };
  expect(await Promise.all([reserveBinding(a), reserveBinding(b)])).toEqual(['saved', 'existing']);
  expect(JSON.parse(localStorage.getItem(bindingStorageKey(a.actorId, a.runId, a.taskId, a.attemptId)!)!)).toEqual(a);
});

test('a browser without coordination cannot start a new request', async () => {
  Object.defineProperty(navigator, 'locks', { configurable: true, value: undefined });
  expect(await reserveBinding(stored())).toBe('unavailable');
  expect(localStorage.length).toBe(0);
});

test('receipt cleanup waits for the slot lock and cannot remove a replacement', async () => {
  const release: Array<() => void> = [];
  Object.defineProperty(navigator, 'locks', { configurable: true, value: { request: (_name: string, _options: object, callback: () => unknown) => new Promise(resolve => {
    release.push(() => resolve(callback()));
  }) } });
  const a = stored();
  const b = { ...a, key: '99999999-1234-1234-1234-123456789abc' };
  const key = bindingStorageKey(a.actorId, a.runId, a.taskId, a.attemptId)!;
  localStorage.setItem(key, JSON.stringify(a));
  const cleanup = removeMatchingBinding(a);
  expect(localStorage.getItem(key)).toBe(JSON.stringify(a));
  localStorage.setItem(key, JSON.stringify(b));
  release.shift()?.();
  expect(await cleanup).toBe(false);
  expect(localStorage.getItem(key)).toBe(JSON.stringify(b));
});

test('a queued reservation aborted by unmount never saves a request', async () => {
  const release: Array<() => void> = [];
  Object.defineProperty(navigator, 'locks', { configurable: true, value: { request: (_name: string, optionsOrCallback: { signal?: AbortSignal } | (() => unknown), maybeCallback?: () => unknown) => {
    const callback = typeof optionsOrCallback === 'function' ? optionsOrCallback : maybeCallback!;
    const signal = typeof optionsOrCallback === 'function' ? undefined : optionsOrCallback.signal;
    return new Promise((resolve, reject) => {
      signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
      release.push(() => resolve(callback()));
    });
  } } });
  const owner = new AbortController();
  const reservation = reserveBinding(stored(), owner.signal);
  owner.abort();
  release.shift()?.();
  expect(await reservation).toBe('unavailable');
  expect(localStorage.length).toBe(0);
});

test('a lost response survives unmount and explicitly retries the exact saved request', async () => {
  const sent: Array<{ body: string; key: string | null }> = [];
  let calls = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    const key = new Headers(init?.headers).get('Idempotency-Key');
    if (!key) return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: 'preview-1', expires_at: new Date(Date.now() + 60_000).toISOString(), action: 'retry_application', eligible: true, reason_code: null, changes: [], retained_evidence: [], budget_impact: { provider_attempts: 0, repair_units: 0 } }));
    sent.push({ body: String(init?.body), key });
    if (++calls === 1) throw new TypeError('lost');
    return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipt_id: 'receipt-1', action: 'retry_application', status: 'applied', observed_at: new Date().toISOString(), reason_code: 'applied' }));
  });
  const props = { runId: 'run-1', taskId: 'task-1', attemptId: 'attempt-1', taskVersion: 2, runVersion: 3, runAllowsExecution: true, taskState: 'decision_pending', pauseRequested: false, cancelRequested: false, unsettledEffects: 0, eligibleActions: ['retry_application'] as const, authorityKey: 'current', onRefresh: vi.fn() };
  const first = renderRecovery(<TaskRecovery {...props} eligibleActions={[...props.eligibleActions]} />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved result' }));
  await userEvent.type(await screen.findByLabelText('Recovery reason'), 'Retained result');
  await userEvent.click(screen.getByRole('button', { name: 'Apply recovery' }));
  await screen.findByText(/could not be confirmed/);
  expect(localStorage.length).toBe(1);
  first.unmount();
  renderRecovery(<TaskRecovery {...props} eligibleActions={[]} taskState="running" runAllowsExecution={false} />);
  await userEvent.click(await screen.findByRole('button', { name: 'Retry same request' }));
  await screen.findByText(/Recovery receipt receipt-1/);
  expect(sent).toHaveLength(2);
  expect(sent[1]).toEqual(sent[0]);
  expect(localStorage.length).toBe(0);
});

test('restored request is isolated by operator and run/task/attempt even when fresh recovery is ineligible', async () => {
  const binding = stored();
  localStorage.setItem(bindingStorageKey(binding.actorId, binding.runId, binding.taskId, binding.attemptId)!, JSON.stringify(binding));
  const fetcher = vi.spyOn(globalThis, 'fetch');
  for (const [actor, changes] of [
    ['other-actor', {}], ['actor-1', { runId: 'other-run' }],
    ['actor-1', { taskId: 'other-task' }], ['actor-1', { attemptId: 'other-attempt' }],
  ] as const) {
    const view = renderRecovery(<TaskRecovery {...recoveryProps} {...changes} />, actor);
    expect(screen.queryByRole('button', { name: 'Retry same request' })).not.toBeInTheDocument();
    view.unmount();
  }
  renderRecovery(<TaskRecovery {...recoveryProps} />);
  expect(await screen.findByRole('button', { name: 'Retry same request' })).toBeEnabled();
  expect(fetcher).not.toHaveBeenCalled();
});

test('corrupt or unavailable storage prevents a new recovery mutation', async () => {
  const key = bindingStorageKey('actor-1', 'run-1', 'task-1', 'attempt-1')!;
  localStorage.setItem(key, '{broken');
  const fetcher = vi.spyOn(globalThis, 'fetch');
  const view = renderRecovery(<TaskRecovery {...recoveryProps} eligibleActions={['retry_application']} taskState="decision_pending" runAllowsExecution unsettledEffects={0} />);
  expect(await screen.findByRole('alert')).toHaveTextContent(/unavailable or unreadable/);
  expect(screen.getByRole('button', { name: 'Retry saved result' })).toBeDisabled();
  expect(fetcher).not.toHaveBeenCalled();
  view.unmount();
  localStorage.removeItem(key);
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('Quota', 'QuotaExceededError'); });
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: 'preview-1', expires_at: new Date(Date.now() + 60_000).toISOString(), action: 'retry_application', eligible: true, reason_code: null, changes: [], retained_evidence: [], budget_impact: { provider_attempts: 0, repair_units: 0 } })));
  renderRecovery(<TaskRecovery {...recoveryProps} eligibleActions={['retry_application']} taskState="decision_pending" runAllowsExecution unsettledEffects={0} />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved result' }));
  await userEvent.type(await screen.findByLabelText('Recovery reason'), 'Try saving first');
  await userEvent.click(screen.getByRole('button', { name: 'Apply recovery' }));
  expect(await screen.findByText(/could not be saved/)).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([, init]) => new Headers(init?.headers).has('Idempotency-Key'))).toHaveLength(0);
});

test('a replaced saved request is not erased by a late receipt from the old request', async () => {
  const initial = stored();
  const key = bindingStorageKey(initial.actorId, initial.runId, initial.taskId, initial.attemptId)!;
  localStorage.setItem(key, JSON.stringify(initial));
  let release!: (response: Response) => void;
  vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise<Response>(resolve => { release = resolve; }));
  renderRecovery(<TaskRecovery {...recoveryProps} />);
  await userEvent.click(await screen.findByRole('button', { name: 'Retry same request' }));
  const replacement = { ...initial, key: '99999999-1234-1234-1234-123456789abc' };
  localStorage.setItem(key, JSON.stringify(replacement));
  release(new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipt_id: 'old-receipt', action: 'retry_application', status: 'applied', observed_at: new Date().toISOString(), reason_code: 'applied' })));
  await screen.findByRole('button', { name: 'Retry same request' });
  expect(JSON.parse(localStorage.getItem(key)!)).toEqual(replacement);
  expect(screen.queryByText(/old-receipt/)).not.toBeInTheDocument();
});

test('authentication failure preserves an exact saved request across remounts with the same actor', async () => {
  const binding = stored();
  const key = bindingStorageKey(binding.actorId, binding.runId, binding.taskId, binding.attemptId)!;
  localStorage.setItem(key, JSON.stringify(binding));
  let calls = 0;
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
    if (++calls === 1) return new Response('{}', { status: 401 });
    return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipt_id: 'renewed-receipt', action: 'retry_application', status: 'applied', observed_at: new Date().toISOString(), reason_code: 'applied' }));
  });
  const first = renderRecovery(<TaskRecovery {...recoveryProps} />);
  await userEvent.click(await screen.findByRole('button', { name: 'Retry same request' }));
  await screen.findByText(/Sign-in expired/);
  expect(localStorage.getItem(key)).toBe(JSON.stringify(binding));
  first.unmount();
  renderRecovery(<TaskRecovery {...recoveryProps} />);
  await userEvent.click(await screen.findByRole('button', { name: 'Retry same request' }));
  await screen.findByText(/Recovery receipt renewed-receipt/);
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(localStorage.getItem(key)).toBeNull();
});

test.each([409, 422])('a restored request rejected with %s clears only its matching slot', async status => {
  const binding = stored();
  const key = bindingStorageKey(binding.actorId, binding.runId, binding.taskId, binding.attemptId)!;
  localStorage.setItem(key, JSON.stringify(binding));
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status }));
  renderRecovery(<TaskRecovery {...recoveryProps} />);
  await userEvent.click(await screen.findByRole('button', { name: 'Retry same request' }));
  expect(await screen.findByText(/preview is stale or expired|request was rejected/i)).toBeInTheDocument();
  expect(localStorage.getItem(key)).toBeNull();
  expect(screen.queryByRole('button', { name: 'Retry same request' })).not.toBeInTheDocument();
});

test('a restored request rejected by authorization remains saved', async () => {
  const binding = stored();
  const key = bindingStorageKey(binding.actorId, binding.runId, binding.taskId, binding.attemptId)!;
  localStorage.setItem(key, JSON.stringify(binding));
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 403 }));
  renderRecovery(<TaskRecovery {...recoveryProps} />);
  await userEvent.click(await screen.findByRole('button', { name: 'Retry same request' }));
  expect(await screen.findByText(/not authorized/i)).toBeInTheDocument();
  expect(localStorage.getItem(key)).toBe(JSON.stringify(binding));
});

test('a new actor sees durable history without the old actor slot, while history never hides its own pending request', async () => {
  const old = stored('old-actor');
  localStorage.setItem(bindingStorageKey(old.actorId, old.runId, old.taskId, old.attemptId)!, JSON.stringify(old));
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipts: [{ receipt_id: 'old-receipt', run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', action: 'retry_application', status: 'applied', observed_at: '2026-09-29T12:00:00Z', reason_code: 'applied' }], has_more: false })));
  const view = renderRecovery(<><TaskRecovery {...recoveryProps} hasRecovery={false} /><RecoveryReceiptHistory runId="run-1" taskId="task-1" attemptId="attempt-1" /></>, 'new-actor');
  expect(await screen.findByText(/Recovery receipt old-receipt/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Retry same request' })).not.toBeInTheDocument();
  view.unmount();
  const pending = stored('new-actor');
  localStorage.setItem(bindingStorageKey(pending.actorId, pending.runId, pending.taskId, pending.attemptId)!, JSON.stringify(pending));
  renderRecovery(<><TaskRecovery {...recoveryProps} hasRecovery={false} /><RecoveryReceiptHistory runId="run-1" taskId="task-1" attemptId="attempt-1" /></>, 'new-actor');
  expect(await screen.findByRole('button', { name: 'Retry same request' })).toBeEnabled();
  expect(await screen.findByText(/Recovery receipt old-receipt/)).toBeInTheDocument();
});

test('receipt still appears when another tab already cleared the matching request', async () => {
  const binding = stored();
  const key = bindingStorageKey(binding.actorId, binding.runId, binding.taskId, binding.attemptId)!;
  localStorage.setItem(key, JSON.stringify(binding));
  let release!: (response: Response) => void;
  vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise<Response>(resolve => { release = resolve; }));
  renderRecovery(<TaskRecovery {...recoveryProps} />);
  await userEvent.click(await screen.findByRole('button', { name: 'Retry same request' }));
  localStorage.removeItem(key);
  release(new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipt_id: 'already-cleared', action: 'retry_application', status: 'applied', observed_at: new Date().toISOString(), reason_code: 'applied' })));
  expect(await screen.findByText(/Recovery receipt already-cleared/)).toBeInTheDocument();
});

test('receipt remains visible when cleanup coordination becomes unavailable', async () => {
  const binding = stored();
  const key = bindingStorageKey(binding.actorId, binding.runId, binding.taskId, binding.attemptId)!;
  localStorage.setItem(key, JSON.stringify(binding));
  let release!: (response: Response) => void;
  vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise<Response>(resolve => { release = resolve; }));
  renderRecovery(<TaskRecovery {...recoveryProps} />);
  await userEvent.click(await screen.findByRole('button', { name: 'Retry same request' }));
  Object.defineProperty(navigator, 'locks', { configurable: true, value: undefined });
  release(new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipt_id: 'uncleared', action: 'retry_application', status: 'applied', observed_at: new Date().toISOString(), reason_code: 'applied' })));
  expect(await screen.findByText(/Recovery receipt uncleared/)).toBeInTheDocument();
  expect(localStorage.getItem(key)).toBe(JSON.stringify(binding));
  expect(screen.queryByRole('button', { name: 'Retry same request' })).not.toBeInTheDocument();
});

test('a new request from another tab replaces a displayed receipt for the old request', async () => {
  const a = stored();
  const b = { ...a, key: '99999999-1234-1234-1234-123456789abc' };
  const key = bindingStorageKey(a.actorId, a.runId, a.taskId, a.attemptId)!;
  localStorage.setItem(key, JSON.stringify(a));
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipt_id: 'receipt-a', action: 'retry_application', status: 'applied', observed_at: new Date().toISOString(), reason_code: 'applied' })));
  renderRecovery(<TaskRecovery {...recoveryProps} />);
  Object.defineProperty(navigator, 'locks', { configurable: true, value: undefined });
  await userEvent.click(await screen.findByRole('button', { name: 'Retry same request' }));
  expect(await screen.findByText(/Recovery receipt receipt-a/)).toBeInTheDocument();
  localStorage.setItem(key, JSON.stringify(b));
  window.dispatchEvent(new StorageEvent('storage', { key, newValue: JSON.stringify(b) }));
  expect(await screen.findByRole('button', { name: 'Retry same request' })).toBeEnabled();
  expect(screen.queryByText(/Recovery receipt receipt-a/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Recovery applied. The receipt is authoritative/)).not.toBeInTheDocument();
});

test('previews eligible recovery, requires reason, and applies with a validated receipt', async () => {
  const now = Date.now();
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if (init?.method === 'POST' && !new Headers(init.headers).has('Idempotency-Key')) return new Response(JSON.stringify({
      run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: 'preview-1', expires_at: new Date(now + 60_000).toISOString(), action: 'retry_application', eligible: true, reason_code: 'prerequisite_changed',
      changes: ['Apply the saved result'], retained_evidence: ['Provider response digest'], budget_impact: { provider_attempts: 0, repair_units: 0 },
    }), { status: 200 });
    return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipt_id: 'receipt-1', action: 'retry_application', status: 'applied', observed_at: new Date(now).toISOString(), reason_code: 'applied' }), { status: 200 });
  });
  renderRecovery(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved result' }));
  await screen.findByText(/Provider response digest/);
  expect(screen.getByText(/Provider attempts: 0/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Apply recovery' }));
  expect(screen.getByText('Operator reason is required')).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText('Recovery reason'), 'Prerequisite is now available');
  await userEvent.click(screen.getByRole('button', { name: 'Apply recovery' }));
  await screen.findByText(/Recovery receipt receipt-1/);
  expect(fetcher.mock.calls.some(([, init]) => init?.method === 'POST' && new Headers(init.headers).get('Idempotency-Key'))).toBe(true);
});

test('retries an unconfirmed apply with the identical body and idempotency key', async () => {
  const sent: Array<{ body: string; key: string | null }> = [];
  let applyCount = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    const key = new Headers(init?.headers).get('Idempotency-Key');
    if (!key) return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: 'preview-1', expires_at: new Date(Date.now() + 60_000).toISOString(), action: 'retry_application', eligible: true, reason_code: null, changes: [], retained_evidence: [], budget_impact: { provider_attempts: 0, repair_units: 0 } }));
    const body = String(init?.body);
    sent.push({ body, key });
    applyCount += 1;
    if (applyCount === 1) throw new TypeError('network lost after send');
    return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipt_id: 'receipt-2', action: 'retry_application', status: 'applied', observed_at: new Date().toISOString(), reason_code: 'applied' }));
  });
  renderRecovery(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved result' }));
  await screen.findByLabelText('Recovery reason');
  await userEvent.type(screen.getByLabelText('Recovery reason'), 'Retry the retained result');
  await userEvent.click(screen.getByRole('button', { name: 'Apply recovery' }));
  await screen.findByText(/could not be confirmed/);
  await userEvent.click(screen.getByRole('button', { name: 'Retry same request' }));
  await screen.findByText(/Recovery receipt receipt-2/);
  expect(sent).toHaveLength(2);
  expect(sent[1]).toEqual(sent[0]);
});

test.each([
  { taskState: 'running', unsettledEffects: 0 },
  { taskState: 'decision_pending', unsettledEffects: 1 },
])('does not preview a server-listed action while execution or effects remain live', async ({ taskState, unsettledEffects }) => {
  const fetcher = vi.spyOn(globalThis, 'fetch');
  renderRecovery(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState={taskState} pauseRequested={false} cancelRequested={false} unsettledEffects={unsettledEffects} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
  expect(screen.getByText(/Recovery is unavailable while work is active/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Retry saved result' })).toBeDisabled();
  expect(fetcher).not.toHaveBeenCalled();
});

test('unknown legacy state with no named server action stays inspectable without recovery controls', () => {
  renderRecovery(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="unknown_state" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={[]} authorityKey="current" onRefresh={vi.fn()} />);
  expect(screen.getByText(/No recovery action is available for the current task state/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Retry|Repair/ })).not.toBeInTheDocument();
});

test.each([
  ['reject_and_retry_step', 'Retry step'],
  ['repair_approved_plan_contract', 'Repair approved-plan instructions'],
] as const)('maps %s to the operator label %s from server eligibility', async (action, label) => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    expect(JSON.parse(String(init?.body))).toEqual({ action });
    return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: 'preview-1', expires_at: new Date(Date.now() + 60_000).toISOString(), action, eligible: true, reason_code: null, changes: [], retained_evidence: [], budget_impact: { provider_attempts: 1, repair_units: action === 'repair_approved_plan_contract' ? 1 : 0 } }));
  });
  renderRecovery(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={[action]} authorityKey="current" onRefresh={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: label }));
  expect(await screen.findByLabelText('Recovery preview')).toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test('an expired preview cannot be applied', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: 'preview-1', expires_at: new Date(Date.now() - 1000).toISOString(), action: 'retry_application', eligible: true, reason_code: null, changes: [], retained_evidence: [], budget_impact: { provider_attempts: 0, repair_units: 0 } })));
  renderRecovery(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved result' }));
  expect(await screen.findByText(/preview is expired or no longer matches current state/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Apply recovery' })).toBeDisabled();
});

test('a preview that expires while the component is mounted is stale as soon as it arrives', async () => {
  let now = 1_800_000_000_000;
  vi.spyOn(Date, 'now').mockImplementation(() => now);
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
    const expiresAt = new Date(now + 60_000).toISOString();
    now += 60_001;
    return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: 'preview-old', expires_at: expiresAt, action: 'retry_application', eligible: true, reason_code: 'eligible', message: 'This result can be retried.', changes: [], retained_evidence: [], budget_impact: { provider_attempts: 0, repair_units: 0 } }));
  });
  renderRecovery(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved result' }));
  expect(await screen.findByText(/preview is expired or no longer matches current state/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Apply recovery' })).toBeDisabled();
});

test('apply rechecks wall time if the browser timer has not fired', async () => {
  let now = Date.now();
  const nowSpy = vi.spyOn(Date, 'now').mockImplementation(() => now);
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if (!new Headers(init?.headers).has('Idempotency-Key')) return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: 'preview-1', expires_at: new Date(now + 60_000).toISOString(), action: 'retry_application', eligible: true, reason_code: 'eligible', message: 'This result can be retried.', changes: [], retained_evidence: [], budget_impact: { provider_attempts: 0, repair_units: 0 } }));
    return new Response('{}');
  });
  renderRecovery(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved result' }));
  await userEvent.type(await screen.findByLabelText('Recovery reason'), 'Check the current expiry');
  now += 60_001;
  nowSpy.mockReturnValue(now);
  await userEvent.click(screen.getByRole('button', { name: 'Apply recovery' }));
  expect(await screen.findByText(/preview has expired/)).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([, init]) => new Headers(init?.headers).has('Idempotency-Key'))).toHaveLength(0);
});

test('an uncertain apply remains replayable with its same key after preview expiry', async () => {
  let now = Date.now();
  const nowSpy = vi.spyOn(Date, 'now').mockImplementation(() => now);
  const sent: Array<{ body: string; key: string | null }> = [];
  let applyCount = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    const key = new Headers(init?.headers).get('Idempotency-Key');
    if (!key) return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: 'preview-1', expires_at: new Date(now + 60_000).toISOString(), action: 'retry_application', eligible: true, reason_code: 'eligible', message: 'This result can be retried.', changes: [], retained_evidence: [], budget_impact: { provider_attempts: 0, repair_units: 0 } }));
    sent.push({ body: String(init?.body), key });
    applyCount += 1;
    if (applyCount === 1) throw new TypeError('network lost after send');
    return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipt_id: 'receipt-after-expiry', action: 'retry_application', status: 'applied', observed_at: new Date().toISOString(), reason_code: 'applied' }));
  });
  renderRecovery(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved result' }));
  await userEvent.type(await screen.findByLabelText('Recovery reason'), 'Retry the identical request');
  await userEvent.click(screen.getByRole('button', { name: 'Apply recovery' }));
  await screen.findByText(/could not be confirmed/);
  now += 60_001;
  nowSpy.mockReturnValue(now);
  await userEvent.click(screen.getByRole('button', { name: 'Retry same request' }));
  await screen.findByText(/Recovery receipt receipt-after-expiry/);
  expect(sent).toHaveLength(2);
  expect(sent[1]).toEqual(sent[0]);
});

test('a stale apply clears the old binding and refreshes authoritative state', async () => {
  const onRefresh = vi.fn();
  let previewRequests = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if (!new Headers(init?.headers).has('Idempotency-Key')) {
      previewRequests += 1;
      return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: `preview-${previewRequests}`, expires_at: new Date(Date.now() + 60_000).toISOString(), action: 'retry_application', eligible: true, reason_code: 'eligible', message: 'Retry this saved result.', changes: [], retained_evidence: [], budget_impact: { provider_attempts: 0, repair_units: 0 } }));
    }
    return new Response('{}', { status: 409 });
  });
  renderRecovery(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={onRefresh} />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved result' }));
  await screen.findByLabelText('Recovery reason');
  await userEvent.type(screen.getByLabelText('Recovery reason'), 'State changed');
  await userEvent.click(screen.getByRole('button', { name: 'Apply recovery' }));
  expect(await screen.findByText(/preview is stale or expired/)).toBeInTheDocument();
  expect(onRefresh).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('button', { name: 'Retry same request' })).not.toBeInTheDocument();
});

test('a mismatched apply receipt is kept unconfirmed', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if (!new Headers(init?.headers).has('Idempotency-Key')) return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: 'preview-1', expires_at: new Date(Date.now() + 60_000).toISOString(), action: 'retry_application', eligible: true, reason_code: 'eligible', message: 'Retry this saved result.', changes: [], retained_evidence: [], budget_impact: { provider_attempts: 0, repair_units: 0 } }));
    return new Response(JSON.stringify({ run_id: 'another-run', task_id: 'task-1', attempt_id: 'attempt-1', receipt_id: 'receipt-bad', action: 'retry_application', status: 'applied', observed_at: new Date().toISOString(), reason_code: 'applied' }));
  });
  renderRecovery(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved result' }));
  await userEvent.type(await screen.findByLabelText('Recovery reason'), 'Check response');
  await userEvent.click(screen.getByRole('button', { name: 'Apply recovery' }));
  expect(await screen.findByText(/could not be confirmed/)).toBeInTheDocument();
  expect(screen.queryByText(/Recovery receipt/)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Retry same request' })).toBeInTheDocument();
});

test('discards a preview response bound to another attempt', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'different-attempt', preview_token: 'preview-1', expires_at: new Date(Date.now() + 60_000).toISOString(), action: 'retry_application', eligible: true, reason_code: null, changes: [], retained_evidence: [], budget_impact: { provider_attempts: 0, repair_units: 0 } })));
  renderRecovery(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved result' }));
  expect(await screen.findByText(/Recovery preview unavailable/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Apply recovery' })).not.toBeInTheDocument();
});

test('disables duplicate apply while the request is in flight', async () => {
  let release!: (response: Response) => void;
  const apply = new Promise<Response>(resolve => { release = resolve; });
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if (!new Headers(init?.headers).has('Idempotency-Key')) return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: 'preview-1', expires_at: new Date(Date.now() + 60_000).toISOString(), action: 'retry_application', eligible: true, reason_code: null, changes: [], retained_evidence: [], budget_impact: { provider_attempts: 0, repair_units: 0 } }));
    return apply;
  });
  renderRecovery(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved result' }));
  await screen.findByLabelText('Recovery reason');
  await userEvent.type(screen.getByLabelText('Recovery reason'), 'Retry once');
  await userEvent.click(screen.getByRole('button', { name: 'Apply recovery' }));
  expect(await screen.findByText('Applying recovery…')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Apply recovery' })).toBeDisabled();
  expect(fetcher.mock.calls.filter(([, init]) => new Headers(init?.headers).has('Idempotency-Key'))).toHaveLength(1);
  release(new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipt_id: 'receipt-3', action: 'retry_application', status: 'applied', observed_at: new Date().toISOString(), reason_code: 'applied' })));
  await screen.findByText(/Recovery receipt receipt-3/);
});
