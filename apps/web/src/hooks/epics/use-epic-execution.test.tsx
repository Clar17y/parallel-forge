import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { renderHook, act, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { useEpicExecution } from './use-epic-execution';
import { api } from '@/lib/api/client';
import type { ExecutionProgress } from './types';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) { super(code); }
  },
}));

describe('useEpicExecution', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';
  const executionId = '22222222-2222-4222-8222-222222222222';
  const progress: ExecutionProgress = {
    schema_version: 1,
    epic_id: epicId,
    epic_version: 7,
    execution_id: executionId,
    execution_version: 3,
    state: 'ACTIVE',
    brief_revision_id: '33333333-3333-4333-8333-333333333333',
    graph_revision_id: '44444444-4444-4444-8444-444444444444',
    active_child: {
      item_id: '66666666-6666-4666-8666-666666666666',
      run_id: '88888888-8888-4888-8888-888888888888',
      run_version: 7,
      run_state: 'AWAITING_PLAN_APPROVAL',
      pending_gate: 'plan',
      pending_evidence_digest: 'f'.repeat(64),
    },
    items: [
      { item_id: '66666666-6666-4666-8666-666666666666', disposition: 'required', status: 'active', blocker_code: null, run_id: '88888888-8888-4888-8888-888888888888' },
      { item_id: '55555555-5555-4555-8555-555555555555', disposition: 'required', status: 'blocked', blocker_code: 'predecessor_integration_unverified', run_id: null },
      { item_id: '77777777-7777-4777-8777-777777777777', disposition: 'deferred', status: 'deferred', blocker_code: null, run_id: null },
    ],
    aggregate_usage: { known_cost_minor: 12, reserved_cost_minor: 40, unknown_usage: false },
  };

  beforeEach(() => { resetEpicMutationStoreForTesting();
    vi.clearAllMocks();
    sessionStorage.clear();
    window.history.replaceState({}, '', '/epics/one');
  });

  afterEach(() => sessionStorage.clear());

  test('loads the frozen projection without inventing process settlement or child collections', async () => {
    vi.mocked(api).mockResolvedValue(progress);
    const { result } = renderHook(() => useEpicExecution(epicId, executionId));
    await act(async () => { await Promise.resolve(); });

    expect(result.current.execution).toEqual(progress);
    expect(result.current.state).toBe('ACTIVE');
    expect(result.current.childRuns).toEqual([progress.active_child]);
    expect(result.current.isPendingDiscovery).toBe(false);
  });

  test('restores execution_id from the URL and retains a start receipt in the URL', async () => {
    window.history.replaceState({}, '', `/epics/${epicId}?execution_id=${executionId}`);
    vi.mocked(api).mockResolvedValue(progress);
    const { result } = renderHook(() => useEpicExecution(epicId));
    await act(async () => { await Promise.resolve(); });
    expect(result.current.executionId).toBe(executionId);

    act(() => result.current.setExecutionId('33333333-3333-4333-8333-333333333333'));
    expect(new URL(window.location.href).searchParams.get('execution_id')).toBe('33333333-3333-4333-8333-333333333333');
  });

  test('follows late route execution IDs and history changes without resetting a local selection prematurely', async () => {
    const other = '99999999-9999-4999-8999-999999999999';
    vi.mocked(api).mockImplementation(async <T,>(path: string) => ({
      ...progress, execution_id: path.endsWith(other) ? other : executionId,
    }) as T);
    const hook = renderHook(({ id }) => useEpicExecution(epicId, id), {
      initialProps: { id: null as string | null },
    });
    expect(hook.result.current.isPendingDiscovery).toBe(true);
    hook.rerender({ id: executionId });
    await waitFor(() => { expect(hook.result.current.execution?.execution_id).toBe(executionId); });
    expect(hook.result.current.executionId).toBe(executionId);

    act(() => { hook.result.current.setExecutionId(other); });
    expect(hook.result.current.executionId).toBe(other);
    await waitFor(() => { expect(hook.result.current.execution?.execution_id).toBe(other); });
    hook.rerender({ id: other });
    expect(hook.result.current.executionId).toBe(other);
    expect(vi.mocked(api).mock.calls.filter(([path]) => path.endsWith(other))).toHaveLength(1);

    hook.rerender({ id: executionId });
    await waitFor(() => { expect(hook.result.current.execution?.execution_id).toBe(executionId); });
    expect(hook.result.current.executionId).toBe(executionId);
    hook.rerender({ id: null });
    expect(hook.result.current.executionId).toBeNull();
    expect(hook.result.current.isPendingDiscovery).toBe(true);
  });

  test('sends a version-bound command without treating the requested transition as settled', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init || init.method === 'GET')) return progress as T;
      if (path === `/epics/${epicId}/executions/${executionId}/commands` && init?.method === 'POST') {
        return { schema_version: 1, action: 'pause', execution_version: 4, state: 'PAUSE_REQUESTED' } as T;
      }
      return undefined as T;
    });
    const { result } = renderHook(() => useEpicExecution(epicId, executionId));
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await result.current.sendCommand('pause', 3); });

    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/executions/${executionId}/commands`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ schema_version: 1, action: 'pause', expected_execution_version: 3 }),
    }));
  });

  test('replays an uncertain command for execution A after remounting on B and restores A', async () => {
    const executionA = executionId;
    const executionB = '99999999-9999-4999-8999-999999999999';
    let commandCalls = 0;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionA}` && (!init?.method || init.method === 'GET')) return progress as T;
      if (path === `/epics/${epicId}/executions/${executionA}/commands` && init?.method === 'POST') {
        commandCalls += 1;
        if (commandCalls === 1) throw new Error('connection lost');
        return { schema_version: 1, action: 'pause', execution_version: 4, state: 'PAUSE_REQUESTED' } as T;
      }
      if (path === `/epics/${epicId}/executions/${executionB}` && (!init?.method || init.method === 'GET')) return { ...progress, execution_id: executionB } as T;
      return undefined as T;
    });

    const first = renderHook(() => useEpicExecution(epicId, executionA));
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await expect(first.result.current.sendCommand('pause', 3)).rejects.toThrow('connection lost'); });
    const original = first.result.current.mutations.pendingMutation;
    expect(original?.path).toBe(`/epics/${epicId}/executions/${executionA}/commands`);
    first.unmount();

    window.history.replaceState({}, '', `/epics/${epicId}?execution_id=${executionB}`);
    vi.mocked(api).mockClear();
    const second = renderHook(() => useEpicExecution(epicId));
    await act(async () => { await second.result.current.mutations.retryPending(); });
    await waitFor(() => expect(second.result.current.executionId).toBe(executionA));
    await waitFor(() => expect(api).toHaveBeenCalledWith(`/epics/${epicId}/executions/${executionA}`, expect.any(Object)));
    expect(new URL(window.location.href).searchParams.get('execution_id')).toBe(executionA);
    const replay = vi.mocked(api).mock.calls.find(([path, init]) => path === original?.path && init?.method === 'POST');
    expect(replay?.[1]).toEqual(expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'Idempotency-Key': original?.idempotencyKey }),
      body: JSON.stringify(original?.body),
    }));
  });
});
