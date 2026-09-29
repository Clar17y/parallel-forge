import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { TaskRecovery } from './task-recovery';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

test('previews eligible recovery, requires reason, and applies with a validated receipt', async () => {
  const now = Date.now();
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if (init?.method === 'POST' && !new Headers(init.headers).has('Idempotency-Key')) return new Response(JSON.stringify({
      run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: 'preview-1', expires_at: new Date(now + 60_000).toISOString(), action: 'retry_application', eligible: true, reason_code: 'prerequisite_changed',
      changes: ['Apply the saved result'], retained_evidence: ['Provider response digest'], budget_impact: { provider_attempts: 0, repair_units: 0 },
    }), { status: 200 });
    return new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', receipt_id: 'receipt-1', action: 'retry_application', status: 'applied', observed_at: new Date(now).toISOString(), reason_code: 'applied' }), { status: 200 });
  });
  render(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
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
  render(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
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
  render(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState={taskState} pauseRequested={false} cancelRequested={false} unsettledEffects={unsettledEffects} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
  expect(screen.getByText(/Recovery is unavailable while work is active/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Retry saved result' })).toBeDisabled();
  expect(fetcher).not.toHaveBeenCalled();
});

test('unknown legacy state with no named server action stays inspectable without recovery controls', () => {
  render(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="unknown_state" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={[]} authorityKey="current" onRefresh={vi.fn()} />);
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
  render(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={[action]} authorityKey="current" onRefresh={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: label }));
  expect(await screen.findByLabelText('Recovery preview')).toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test('an expired preview cannot be applied', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'attempt-1', preview_token: 'preview-1', expires_at: new Date(Date.now() - 1000).toISOString(), action: 'retry_application', eligible: true, reason_code: null, changes: [], retained_evidence: [], budget_impact: { provider_attempts: 0, repair_units: 0 } })));
  render(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
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
  render(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
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
  render(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
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
  render(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
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
  render(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={onRefresh} />);
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
  render(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved result' }));
  await userEvent.type(await screen.findByLabelText('Recovery reason'), 'Check response');
  await userEvent.click(screen.getByRole('button', { name: 'Apply recovery' }));
  expect(await screen.findByText(/could not be confirmed/)).toBeInTheDocument();
  expect(screen.queryByText(/Recovery receipt/)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Retry same request' })).toBeInTheDocument();
});

test('discards a preview response bound to another attempt', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ run_id: 'run-1', task_id: 'task-1', attempt_id: 'different-attempt', preview_token: 'preview-1', expires_at: new Date(Date.now() + 60_000).toISOString(), action: 'retry_application', eligible: true, reason_code: null, changes: [], retained_evidence: [], budget_impact: { provider_attempts: 0, repair_units: 0 } })));
  render(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
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
  render(<TaskRecovery runId="run-1" taskId="task-1" attemptId="attempt-1" taskVersion={2} runVersion={3} runAllowsExecution taskState="decision_pending" pauseRequested={false} cancelRequested={false} unsettledEffects={0} eligibleActions={['retry_application']} authorityKey="current" onRefresh={vi.fn()} />);
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
