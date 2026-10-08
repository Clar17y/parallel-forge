import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { renderHook, act, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { useEpicExecution } from './use-epic-execution';
import { api } from '@/lib/api/client';
import type { EpicExecutionProjection } from './types';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) { super(code); }
  },
}));

describe('useEpicExecution', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';
  const executionId = '22222222-2222-4222-8222-222222222222';
  const projection: EpicExecutionProjection = {
    execution: {
      epic_id: epicId,
      execution_id: executionId,
      brief_revision_id: '33333333-3333-4333-8333-333333333333',
      brief_digest: 'b'.repeat(64),
      graph_revision_id: '44444444-4444-4444-8444-444444444444',
      graph_digest: 'g'.repeat(64),
      created_at: '2026-10-01T00:00:00Z',
    },
    control_version: 3,
    control_state: 'ACTIVE',
    blocker_code: null,
    children: [
      {
        attempt: {
          actor_id: 'act-1',
          actual_epic_version: 7,
          attempt_id: 'att-1',
          attempt_number: 1,
          base_ref: 'main',
          base_sha: 'a'.repeat(40),
          blocker_codes: [],
          brief_digest: 'b'.repeat(64),
          brief_revision_id: '33333333-3333-4333-8333-333333333333',
          context_digest: 'c'.repeat(64),
          created_at: '2026-10-01T00:00:00Z',
          dependency_evidence: [],
          epic_id: epicId,
          execution_id: executionId,
          expected_epic_version: 7,
          graph_digest: 'g'.repeat(64),
          graph_revision_id: '44444444-4444-4444-8444-444444444444',
          item_digest: 'i'.repeat(64),
          item_disposition: 'required',
          item_id: '66666666-6666-4666-8666-666666666666',
          override_note: null,
          owner_override: false,
          run_id: '88888888-8888-4888-8888-888888888888',
          task_digest: 't'.repeat(64),
          task_id: 'task-1',
        },
        run_version: 7,
        run_state: 'AWAITING_PLAN_APPROVAL',
        effects_settled: false,
        pending_gate: 'plan',
        retained_gate: null,
      },
    ],
    intents: [],
    owner_actions: [],
    items: [],
  };

  beforeEach(() => {
    resetEpicMutationStoreForTesting();
    vi.clearAllMocks();
    sessionStorage.clear();
    window.history.replaceState({}, '', '/epics/one');
  });

  afterEach(() => sessionStorage.clear());

  test('loads the exact generated projection without inventing process settlement or compatibility collections', async () => {
    vi.mocked(api).mockResolvedValue(projection);
    const { result } = renderHook(() => useEpicExecution(epicId, executionId));
    await act(async () => { await Promise.resolve(); });

    expect(result.current.execution).toEqual(projection);
    expect(result.current.controlState).toBe('ACTIVE');
    expect(result.current.children).toEqual(projection.children);
    expect(result.current.isPendingDiscovery).toBe(false);
  });

  test('restores execution_id from the URL and retains a start receipt in the URL', async () => {
    window.history.replaceState({}, '', `/epics/${epicId}?execution_id=${executionId}`);
    vi.mocked(api).mockResolvedValue(projection);
    const { result } = renderHook(() => useEpicExecution(epicId));
    await act(async () => { await Promise.resolve(); });
    expect(result.current.executionId).toBe(executionId);

    act(() => result.current.setExecutionId('33333333-3333-4333-8333-333333333333'));
    expect(new URL(window.location.href).searchParams.get('execution_id')).toBe('33333333-3333-4333-8333-333333333333');
  });

  test('follows late route execution IDs and history changes without resetting a local selection prematurely', async () => {
    const other = '99999999-9999-4999-8999-999999999999';
    vi.mocked(api).mockImplementation(async <T,>(path: string) => ({
      ...projection,
      execution: {
        ...projection.execution,
        execution_id: path.endsWith(other) ? other : executionId,
      },
    }) as T);
    const hook = renderHook(({ id }) => useEpicExecution(epicId, id), {
      initialProps: { id: null as string | null },
    });
    expect(hook.result.current.isPendingDiscovery).toBe(true);
    hook.rerender({ id: executionId });
    await waitFor(() => { expect(hook.result.current.execution?.execution.execution_id).toBe(executionId); });
    expect(hook.result.current.executionId).toBe(executionId);

    act(() => { hook.result.current.setExecutionId(other); });
    expect(hook.result.current.executionId).toBe(other);
    await waitFor(() => { expect(hook.result.current.execution?.execution.execution_id).toBe(other); });
    hook.rerender({ id: other });
    expect(hook.result.current.executionId).toBe(other);
    expect(vi.mocked(api).mock.calls.filter(([path]) => path.endsWith(other))).toHaveLength(1);

    hook.rerender({ id: executionId });
    await waitFor(() => { expect(hook.result.current.execution?.execution.execution_id).toBe(executionId); });
    expect(hook.result.current.executionId).toBe(executionId);
    hook.rerender({ id: null });
    expect(hook.result.current.executionId).toBeNull();
    expect(hook.result.current.isPendingDiscovery).toBe(true);
  });

  test('sends a version-bound command without treating the requested transition as settled', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init || init.method === 'GET')) return projection as T;
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
      if (path === `/epics/${epicId}/executions/${executionA}` && (!init?.method || init.method === 'GET')) return projection as T;
      if (path === `/epics/${epicId}/executions/${executionA}/commands` && init?.method === 'POST') {
        commandCalls += 1;
        if (commandCalls === 1) throw new Error('connection lost');
        return { schema_version: 1, action: 'pause', execution_version: 4, state: 'PAUSE_REQUESTED' } as T;
      }
      if (path === `/epics/${epicId}/executions/${executionB}` && (!init?.method || init.method === 'GET')) {
        return { ...projection, execution: { ...projection.execution, execution_id: executionB } } as T;
      }
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

  test('discovers current execution from GET /epics/{epicId}/executions when no initial ID is supplied', async () => {
    const projection1: EpicExecutionProjection = {
      execution: {
        epic_id: epicId,
        execution_id: 'old-exec-id',
        brief_revision_id: 'b1',
        brief_digest: 'bd1',
        graph_revision_id: 'g1',
        graph_digest: 'gd1',
        created_at: '2026-10-01T00:00:00Z',
      },
      control_version: 1,
      control_state: 'completed',
      blocker_code: null,
      children: [],
      intents: [],
      owner_actions: [],
      items: [],
    };
    const projection2: EpicExecutionProjection = {
      execution: {
        epic_id: epicId,
        execution_id: executionId,
        brief_revision_id: 'b2',
        brief_digest: 'bd2',
        graph_revision_id: 'g2',
        graph_digest: 'gd2',
        created_at: '2026-10-02T00:00:00Z',
      },
      control_version: 2,
      control_state: 'active',
      blocker_code: null,
      children: [],
      intents: [],
      owner_actions: [],
      items: [],
    };

    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}/executions`) return [projection1, projection2] as T;
      if (path === `/epics/${epicId}/executions/${executionId}`) return projection2 as T;
      return undefined as T;
    });

    const { result } = renderHook(() => useEpicExecution(epicId));
    await waitFor(() => expect(result.current.executionId).toBe(executionId));
    expect(result.current.executions).toHaveLength(2);
    expect(result.current.controlState).toBe('active');
  });

  test('truthfully reflects null legacy control sidecar without guessing version 1', async () => {
    const legacyProjection: EpicExecutionProjection = {
      execution: {
        epic_id: epicId,
        execution_id: 'legacy-id',
        brief_revision_id: 'b-leg',
        brief_digest: 'bd-leg',
        graph_revision_id: 'g-leg',
        graph_digest: 'gd-leg',
        created_at: '2026-09-01T00:00:00Z',
      },
      control_version: null,
      control_state: null,
      blocker_code: null,
      children: [],
      intents: [],
      owner_actions: [],
      items: [],
    };

    vi.mocked(api).mockResolvedValue(legacyProjection);
    const { result } = renderHook(() => useEpicExecution(epicId, 'legacy-id'));
    await waitFor(() => expect(result.current.execution).not.toBeNull());

    expect(result.current.controlVersion).toBeNull();
    expect(result.current.controlState).toBeNull();
  });

  test('supports starting a second epoch after auto-discovering the first execution', async () => {
    const epoch1: EpicExecutionProjection = {
      execution: {
        epic_id: epicId,
        execution_id: 'epoch-1-id',
        brief_revision_id: 'b1',
        brief_digest: 'bd1',
        graph_revision_id: 'g1',
        graph_digest: 'gd1',
        created_at: '2026-10-01T00:00:00Z',
      },
      control_version: 1,
      control_state: 'completed',
      blocker_code: null,
      children: [],
      intents: [],
      owner_actions: [],
      items: [],
    };
    const epoch2Snapshot = {
      epic_id: epicId,
      execution_id: 'epoch-2-id',
      brief_revision_id: 'b2',
      brief_digest: 'bd2',
      graph_revision_id: 'g2',
      graph_digest: 'gd2',
      created_at: '2026-10-02T00:00:00Z',
    };
    const epoch2: EpicExecutionProjection = {
      execution: epoch2Snapshot,
      control_version: 1,
      control_state: 'active',
      blocker_code: null,
      children: [],
      intents: [],
      owner_actions: [],
      items: [],
    };

    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions` && (!init?.method || init.method === 'GET')) return [epoch1] as T;
      if (path === `/epics/${epicId}/executions/epoch-1-id`) return epoch1 as T;
      if (path === `/epics/${epicId}/executions` && init?.method === 'POST') return epoch2Snapshot as T;
      if (path === `/epics/${epicId}/executions/epoch-2-id`) return epoch2 as T;
      return undefined as T;
    });

    const { result } = renderHook(() => useEpicExecution(epicId));
    await waitFor(() => expect(result.current.executionId).toBe('epoch-1-id'));

    // Start a new epoch (second execution) while epoch 1 is active
    await act(async () => {
      await result.current.startExecution(7);
    });

    await waitFor(() => expect(result.current.executionId).toBe('epoch-2-id'));
  });

  test('starts execution with alternate matching saved pair and owner override with note', async () => {
    const snapshot = {
      epic_id: epicId,
      execution_id: 'new-exec-id',
      brief_revision_id: 'b-custom',
      brief_digest: 'bd-custom',
      graph_revision_id: 'g-custom',
      graph_digest: 'gd-custom',
      created_at: '2026-10-03T00:00:00Z',
    };
    const newProjection: EpicExecutionProjection = {
      execution: snapshot,
      control_version: 1,
      control_state: 'active',
      blocker_code: null,
      children: [],
      intents: [],
      owner_actions: [],
      items: [],
    };

    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions` && init?.method === 'POST') return snapshot as T;
      if (path === `/epics/${epicId}/executions/new-exec-id`) return newProjection as T;
      return undefined as T;
    });

    const { result } = renderHook(() => useEpicExecution(epicId));

    await act(async () => {
      await result.current.startExecution({
        expectedEpicVersion: 7,
        briefRevisionId: 'b-custom',
        briefDigest: 'bd-custom',
        graphRevisionId: 'g-custom',
        graphDigest: 'gd-custom',
        ownerOverride: true,
        overrideNote: 'Owner authorized test revision',
      });
    });

    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/executions`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        schema_version: 1,
        expected_epic_version: 7,
        owner_override: true,
        brief_revision_id: 'b-custom',
        brief_digest: 'bd-custom',
        graph_revision_id: 'g-custom',
        graph_digest: 'gd-custom',
        override_note: 'Owner authorized test revision',
      }),
    }));

    await waitFor(() => expect(result.current.executionId).toBe('new-exec-id'));
  });

  test('does not poll discovery collection recurringly while selected progress stays fresh', async () => {
    vi.useFakeTimers();
    try {
      let executionsCalls = 0;
      let executionCalls = 0;
      vi.mocked(api).mockImplementation(async <T,>(path: string) => {
        if (path === `/epics/${epicId}/executions`) {
          executionsCalls += 1;
          return [projection] as T;
        }
        if (path === `/epics/${epicId}/executions/${executionId}`) {
          executionCalls += 1;
          return projection as T;
        }
        return undefined as T;
      });

      const { result } = renderHook(() => useEpicExecution(epicId, executionId));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      expect(executionsCalls).toBe(1);
      expect(executionCalls).toBe(1);

      // Advance across several discovery periods (15 seconds) in steps
      for (let i = 0; i < 5; i++) {
        await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
      }

      // Selected execution progress polled (every 3s), but discovery collection did NOT poll
      expect(executionCalls).toBeGreaterThanOrEqual(4);
      expect(executionsCalls).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  test('explicit discovery refresh on empty or failed discovery sees new execution', async () => {
    let executionsCalls = 0;
    let shouldFail = true;
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}/executions`) {
        executionsCalls += 1;
        if (shouldFail) {
          throw new Error('discovery temporary failure');
        }
        return [projection] as T;
      }
      if (path === `/epics/${epicId}/executions/${executionId}`) {
        return projection as T;
      }
      return undefined as T;
    });

    const { result } = renderHook(() => useEpicExecution(epicId));
    await waitFor(() => expect(result.current.executionsFailed).toBe(true));
    expect(result.current.isPendingDiscovery).toBe(true);
    expect(executionsCalls).toBe(1);

    // Explicit discovery refresh
    shouldFail = false;
    await act(async () => {
      result.current.refreshExecutions();
    });

    await waitFor(() => expect(result.current.executionsFailed).toBe(false));
    await waitFor(() => expect(result.current.executionId).toBe(executionId));
    expect(result.current.isPendingDiscovery).toBe(false);
    expect(executionsCalls).toBe(2);
  });
});
