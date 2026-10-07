import { renderHook, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { startTransition, useEffect, useLayoutEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { EpicMutationProvider, useEpicMutations, resetEpicMutationStoreForTesting } from './use-epic-mutations';
import { api, ApiError } from '@/lib/api/client';
import type { EpicLaunchConflictDetail } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}, public detail?: unknown) {
      super(code);
    }
  },
}));

describe('useEpicMutations', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api).mockReset();
    sessionStorage.clear();
    resetEpicMutationStoreForTesting();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
    resetEpicMutationStoreForTesting();
  });

  test('successfully executes mutation and sends frozen idempotency key and explicit expected_epic_version', async () => {
    vi.mocked(api).mockResolvedValueOnce({ epic_id: epicId, version: 2 });

    const { result } = renderHook(() => useEpicMutations(epicId));

    let res: unknown;
    await act(async () => {
      res = await result.current.execute('PATCH', `/epics/${epicId}`, {
        schema_version: 1,
        expected_epic_version: 1,
        title: 'Updated Title',
        draft: { problem: 'New Problem' },
      });
    });

    expect(res).toEqual({ epic_id: epicId, version: 2 });
    expect(api).toHaveBeenCalledTimes(1);
    const callArgs = vi.mocked(api).mock.calls[0];
    expect(callArgs[0]).toBe(`/epics/${epicId}`);
    expect(callArgs[1]?.method).toBe('PATCH');
    const headers = callArgs[1]?.headers as Record<string, string>;
    expect(headers['Idempotency-Key']).toBeDefined();
    expect(JSON.parse(callArgs[1]?.body as string)).toEqual({
      schema_version: 1,
      expected_epic_version: 1,
      title: 'Updated Title',
      draft: { problem: 'New Problem' },
    });
    expect(result.current.hasPendingRetry).toBe(false);
    expect(result.current.error).toBeNull();
  });

  test('retains exact frozen key and body across ambiguous failure and retries exact original request', async () => {
    // First call rejects with network failure
    vi.mocked(api).mockRejectedValueOnce(new Error('Failed to fetch'));

    const { result } = renderHook(() => useEpicMutations(epicId));

    await act(async () => {
      try {
        await result.current.execute('POST', `/epics/${epicId}/brief-revisions`, {
          schema_version: 1,
          expected_epic_version: 2,
          content: { problem: 'Important problem' },
        });
      } catch {
        // Expected error
      }
    });

    expect(result.current.hasPendingRetry).toBe(true);
    expect(result.current.error).toContain('uncertain');
    expect(api).toHaveBeenCalledTimes(1);
    const firstCall = vi.mocked(api).mock.calls[0];
    const initialKey = (firstCall[1]?.headers as Record<string, string>)['Idempotency-Key'];
    const initialBody = firstCall[1]?.body;

    // Retry the pending request
    vi.mocked(api).mockResolvedValueOnce({ brief_revision_id: 'rev-1', content_digest: 'd1' });
    await act(async () => {
      await result.current.retryPending();
    });

    expect(api).toHaveBeenCalledTimes(2);
    const secondCall = vi.mocked(api).mock.calls[1];
    expect(secondCall[0]).toBe(`/epics/${epicId}/brief-revisions`);
    expect((secondCall[1]?.headers as Record<string, string>)['Idempotency-Key']).toBe(initialKey);
    expect(secondCall[1]?.body).toBe(initialBody);
    expect(result.current.hasPendingRetry).toBe(false);
    expect(result.current.error).toBeNull();
  });

  test('retains a failed create across remount and completes the original action once', async () => {
    const created = vi.fn();
    vi.mocked(api).mockRejectedValueOnce(new Error('response lost'));
    const first = renderHook(() => useEpicMutations('create'));
    await act(async () => {
      await expect(first.result.current.execute('POST', '/epics', { title: 'Original' }, { kind: 'create' })).rejects.toThrow();
    });
    const original = vi.mocked(api).mock.calls[0][1];
    first.unmount();
    const second = renderHook(() => useEpicMutations('create'));
    expect(second.result.current.hasPendingRetry).toBe(true);
    expect(second.result.current.pendingMutation?.kind).toBe('create');
    await act(async () => {
      await expect(second.result.current.execute('POST', '/epics', { title: 'Changed' }, { kind: 'create' })).rejects.toThrow(/competing mutation/i);
    });
    vi.mocked(api).mockResolvedValueOnce({ epic_id: epicId });
    await act(async () => {
      const completion = await second.result.current.retryPending();
      created(completion);
    });
    expect(created).toHaveBeenCalledWith({ epic_id: epicId });
    expect(vi.mocked(api).mock.calls[1][1]?.body).toBe(original?.body);
    expect((vi.mocked(api).mock.calls[1][1]?.headers as Record<string, string>)['Idempotency-Key']).toBe(
      (original?.headers as Record<string, string>)['Idempotency-Key']
    );
  });

  test('restores pending mutation from sessionStorage on remount/reload and prevents competing mutations', async () => {
    // Simulate pending mutation in sessionStorage before mount
    const frozenPayload = {
      method: 'POST',
      path: `/epics/${epicId}/brief-adoptions`,
      body: {
        schema_version: 1,
        expected_epic_version: 3,
        brief_revision_id: 'rev-3',
        brief_digest: 'a'.repeat(64),
      },
      idempotencyKey: 'test-key',
      timestamp: Date.now(),
    };
    sessionStorage.setItem(`epic_pending_mutation_${epicId}`, JSON.stringify(frozenPayload));

    // Mount hook
    const { result } = renderHook(() => useEpicMutations(epicId));

    expect(result.current.hasPendingRetry).toBe(true);

    // Attempting a competing mutation while uncertain should throw or be blocked
    await act(async () => {
      await expect(
        result.current.execute('PATCH', `/epics/${epicId}`, { schema_version: 1, expected_epic_version: 3, title: 'Other' })
      ).rejects.toThrow(/competing mutation/i);
    });

    // Retrying uses the restored key and body from sessionStorage
    vi.mocked(api).mockResolvedValueOnce({ epic_id: epicId, version: 4 });
    await act(async () => {
      await result.current.retryPending();
    });

    expect(api).toHaveBeenCalledTimes(1);
    const callArgs = vi.mocked(api).mock.calls[0];
    expect(callArgs[0]).toBe(`/epics/${epicId}/brief-adoptions`);
    expect((callArgs[1]?.headers as Record<string, string>)['Idempotency-Key']).toBe('test-key');
    expect(JSON.parse(callArgs[1]?.body as string)).toEqual(frozenPayload.body);
    expect(sessionStorage.getItem(`epic_pending_mutation_${epicId}`)).toBeNull();
    expect(result.current.hasPendingRetry).toBe(false);
  });

  test('clears pending mutation on 409 stale conflict and exposes conflict state', async () => {
    vi.mocked(api).mockRejectedValueOnce(new ApiError(409, 'stale-projection'));

    const { result } = renderHook(() => useEpicMutations(epicId));

    await act(async () => {
      try {
        await result.current.execute('POST', `/epics/${epicId}/brief-adoptions`, {
          schema_version: 1,
          expected_epic_version: 1,
          brief_revision_id: 'rev-1',
          brief_digest: 'd1',
        });
      } catch {
        // Expected
      }
    });

    expect(result.current.hasPendingRetry).toBe(false);
    expect(result.current.conflict).toBe(true);
    expect(sessionStorage.getItem(`epic_pending_mutation_${epicId}`)).toBeNull();
  });

  test('treats a typed epic launch blocker as a definitive workflow rejection, not a version conflict', async () => {
    const detail: EpicLaunchConflictDetail = { code: 'epic_launch_blocked', blocker_codes: ['item_deferred'], actual_epic_version: 7, owner_action: 'retry_with_owner_override' };
    vi.mocked(api).mockRejectedValueOnce(new ApiError(409, 'epic_launch_blocked', {}, detail));
    const { result } = renderHook(() => useEpicMutations(epicId));

    await act(async () => {
      await expect(result.current.execute('POST', `/epics/${epicId}/work-item-runs`, { owner_override: false }, { kind: 'work-item-launch' })).rejects.toThrow();
    });

    expect(result.current.hasPendingRetry).toBe(false);
    expect(result.current.conflict).toBe(false);
    expect(result.current.errorDetail).toEqual(detail);
    expect(result.current.error).toMatch(/blocked this work-item launch/i);
    expect(sessionStorage.getItem(`epic_pending_mutation_${epicId}`)).toBeNull();
  });

  test('keeps the unresolved request in memory and reports unavailable reload protection when storage fails', async () => {
    const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError');
    });
    vi.mocked(api).mockRejectedValueOnce(new Error('response lost'));
    const { result } = renderHook(() => useEpicMutations(epicId));
    await act(async () => {
      await expect(result.current.execute('PATCH', `/epics/${epicId}`, {
        schema_version: 1,
        expected_epic_version: 1,
        title: 'Title',
      })).rejects.toThrow('response lost');
      await expect(result.current.execute('POST', '/epics', {})).rejects.toThrow(/unresolved/);
    });
    expect(write).toHaveBeenCalled();
    expect(result.current.hasPendingRetry).toBe(true);
    expect(result.current.reloadProtected).toBe(false);
    expect(api).toHaveBeenCalledTimes(1);
    write.mockRestore();
  });

  test('blocks a competing write before React rerenders after a lost response', async () => {
    vi.mocked(api).mockRejectedValueOnce(new Error('response lost')).mockResolvedValueOnce({ version: 3 });
    const { result } = renderHook(() => useEpicMutations(epicId));
    const execute = result.current.execute;
    await act(async () => {
      await expect(execute('PATCH', `/epics/${epicId}`, { title: 'Original' }, { kind: 'brief-draft' })).rejects.toThrow('response lost');
      await expect(execute('POST', `/epics/${epicId}/executions`, {}, { kind: 'execution-start' })).rejects.toThrow(/unresolved/i);
    });
    expect(api).toHaveBeenCalledTimes(1);
  });

  test('restores the original action and runs its registered completion after a real failure and remount', async () => {
    const completion = vi.fn();
    const useCreate = () => {
      const mutations = useEpicMutations();
      const { registerCompletion } = mutations;
      useEffect(() => registerCompletion('create', completion), [registerCompletion]);
      return mutations;
    };
    vi.mocked(api).mockRejectedValueOnce(new Error('response lost'));
    const first = renderHook(useCreate);
    await act(async () => {
      await expect(first.result.current.execute('POST', '/epics', { title: 'Original' }, { kind: 'create' })).rejects.toThrow();
    });
    const original = vi.mocked(api).mock.calls[0];
    first.unmount();
    const next = renderHook(useCreate);
    expect(next.result.current.hasPendingRetry).toBe(true);
    vi.mocked(api).mockResolvedValueOnce({ epic_id: epicId });
    await act(async () => { await next.result.current.retryPending(); });
    expect(vi.mocked(api).mock.calls[1]).toEqual(original);
    expect(completion).toHaveBeenCalledTimes(1);
    expect(completion).toHaveBeenCalledWith({ epic_id: epicId }, expect.objectContaining({ kind: 'create' }));
  });

  test('shares one unresolved operation between all epic sections', async () => {
    const { result } = renderHook(() => ({ brief: useEpicMutations(epicId), delivery: useEpicMutations(epicId) }), {
      wrapper: ({ children }) => <EpicMutationProvider epicId={epicId}>{children}</EpicMutationProvider>,
    });
    vi.mocked(api).mockRejectedValueOnce(new ApiError(503, 'unavailable'));
    await act(async () => { await expect(result.current.brief.execute('PATCH', `/epics/${epicId}`, {}, { kind: 'brief-draft' })).rejects.toThrow(); });
    expect(result.current.delivery.pendingMutation).toBe(result.current.brief.pendingMutation);
    await act(async () => { await expect(result.current.delivery.execute('POST', `/epics/${epicId}/executions`, {}, { kind: 'execution-start' })).rejects.toThrow(/unresolved/i); });
    expect(api).toHaveBeenCalledTimes(1);
  });

  test('keeps pending requests associated with their original epic when the target changes', async () => {
    vi.mocked(api).mockRejectedValueOnce(new Error('lost'));
    const hook = renderHook(({ id }) => useEpicMutations(id), { initialProps: { id: epicId } });
    await act(async () => { await expect(hook.result.current.execute('PATCH', `/epics/${epicId}`, { title: 'A' })).rejects.toThrow(); });
    hook.rerender({ id: 'other-epic' });
    expect(hook.result.current.hasPendingRetry).toBe(false);
    hook.rerender({ id: epicId });
    expect(hook.result.current.pendingMutation?.path).toBe(`/epics/${epicId}`);
  });

  test('a later unavailable response cannot erase an earlier uncertain request', async () => {
    vi.mocked(api).mockRejectedValueOnce(new Error('response lost')).mockRejectedValueOnce(new ApiError(404, 'producer-unavailable'));
    const hook = renderHook(() => useEpicMutations(epicId));
    await act(async () => { await expect(hook.result.current.execute('POST', `/epics/${epicId}/executions`, {}, { kind: 'execution-start' })).rejects.toThrow(); });
    await act(async () => { await expect(hook.result.current.retryPending()).rejects.toThrow(); });
    expect(hook.result.current.hasPendingRetry).toBe(true);
    expect(vi.mocked(api).mock.calls[1]).toEqual(vi.mocked(api).mock.calls[0]);
  });

  test('definitive validation errors expose affected fields and permit a corrected request', async () => {
    vi.mocked(api).mockRejectedValueOnce(new ApiError(422, 'invalid', { 'items.0.acceptance_criteria': 'Required.' }));
    const hook = renderHook(() => useEpicMutations(epicId));
    await act(async () => { await expect(hook.result.current.execute('POST', `/epics/${epicId}/graph-revisions`, {})).rejects.toThrow(); });
    expect(hook.result.current.hasPendingRetry).toBe(false);
    expect(hook.result.current.error).toContain('items.0.acceptance_criteria: Required.');
  });

  test('a completion rendering failure does not turn a known server success into an uncertain request', async () => {
    vi.mocked(api).mockResolvedValueOnce({ version: 2 });
    const hook = renderHook(() => useEpicMutations(epicId));
    hook.result.current.registerCompletion('brief-draft', () => { throw new Error('view failure'); });
    await act(async () => { await hook.result.current.execute('PATCH', `/epics/${epicId}`, {}, { kind: 'brief-draft' }); });
    expect(hook.result.current.hasPendingRetry).toBe(false);
    expect(hook.result.current.error).toContain('was saved');
  });

  test('a receipt arriving after its page unmounts retains the request for the next owner to consume', async () => {
    let finish!: (value: unknown) => void;
    vi.mocked(api).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const completion = vi.fn();
    const useStart = () => {
      const mutations = useEpicMutations(epicId);
      const { registerCompletion } = mutations;
      useEffect(() => registerCompletion('execution-start', completion), [registerCompletion]);
      return mutations;
    };
    const first = renderHook(useStart);
    let submitted!: Promise<unknown>;
    act(() => { submitted = first.result.current.execute('POST', `/epics/${epicId}/executions`, { expected_epic_version: 1 }, { kind: 'execution-start' }); });
    const original = vi.mocked(api).mock.calls[0];
    first.unmount();
    const receipt = { execution_id: 'saved-execution', execution_version: 1 };
    await act(async () => { finish(receipt); await submitted; });
    expect(sessionStorage.getItem(`epic_pending_mutation_${epicId}`)).not.toBeNull();
    expect(completion).not.toHaveBeenCalled();
    const second = renderHook(useStart);
    vi.mocked(api).mockResolvedValueOnce(receipt);
    await act(async () => { await second.result.current.retryPending(); });
    expect(vi.mocked(api).mock.calls[1]).toEqual(original);
    expect(completion).toHaveBeenCalledTimes(1);
    expect(completion).toHaveBeenCalledWith(receipt, expect.objectContaining({ kind: 'execution-start' }));
  });

  test.each([400, 401, 403, 404])('a restored request before catch retains its identity through retry %s and a later receipt', async (status) => {
    const rawPending = {
      method: 'POST',
      path: `/epics/${epicId}/executions`,
      body: { expected_epic_version: 1 },
      idempotencyKey: 'saved-before-catch-key',
      timestamp: Date.now(),
      kind: 'execution-start',
    };
    sessionStorage.setItem(`epic_pending_mutation_${epicId}`, JSON.stringify(rawPending));

    const hook = renderHook(() => useEpicMutations(epicId));
    expect(hook.result.current.hasPendingRetry).toBe(true);
    expect(hook.result.current.pendingMutation?.uncertain).toBe(true);

    vi.mocked(api).mockRejectedValueOnce(new ApiError(status, 'rejected-replay'));
    await act(async () => {
      await expect(hook.result.current.retryPending()).rejects.toThrow();
    });

    expect(hook.result.current.hasPendingRetry).toBe(true);
    expect(hook.result.current.pendingMutation?.idempotencyKey).toBe('saved-before-catch-key');
    expect(sessionStorage.getItem(`epic_pending_mutation_${epicId}`)).not.toBeNull();

    vi.mocked(api).mockRejectedValueOnce(new ApiError(404, 'not_found'));
    await act(async () => {
      await expect(hook.result.current.retryPending()).rejects.toThrow();
    });
    expect(hook.result.current.hasPendingRetry).toBe(true);

    const completion = vi.fn();
    hook.result.current.registerCompletion('execution-start', completion);
    const receipt = { execution_id: 'recovered-execution-id', execution_version: 1 };
    vi.mocked(api).mockResolvedValueOnce(receipt);
    await act(async () => {
      await hook.result.current.retryPending();
    });
    expect(hook.result.current.hasPendingRetry).toBe(false);
    expect(completion).toHaveBeenCalledTimes(1);
    expect(completion).toHaveBeenCalledWith(receipt, expect.objectContaining({ idempotencyKey: 'saved-before-catch-key' }));
    for (const [path, init] of vi.mocked(api).mock.calls) {
      expect(path).toBe(rawPending.path);
      expect(init?.method).toBe(rawPending.method);
      expect(init?.body).toBe(JSON.stringify(rawPending.body));
      expect((init?.headers as Record<string, string>)['Idempotency-Key']).toBe(rawPending.idempotencyKey);
    }
    expect(completion.mock.calls[0][1].timestamp).toBe(rawPending.timestamp);
  });

  test.each([400, 401, 403, 404])('a fresh definitive %s clears pending and permits a corrected action', async (status) => {
    vi.mocked(api).mockRejectedValueOnce(new ApiError(status, 'rejected-fresh'));
    const hook = renderHook(() => useEpicMutations(epicId));
    await act(async () => {
      await expect(hook.result.current.execute('POST', `/epics/${epicId}/brief-revisions`, { problem: '' }, { kind: 'brief-revision' })).rejects.toThrow();
    });
    expect(hook.result.current.hasPendingRetry).toBe(false);
    expect(sessionStorage.getItem(`epic_pending_mutation_${epicId}`)).toBeNull();

    vi.mocked(api).mockResolvedValueOnce({ brief_revision_id: 'rev-ok' });
    await act(async () => {
      await hook.result.current.execute('POST', `/epics/${epicId}/brief-revisions`, { problem: 'valid' }, { kind: 'brief-revision' });
    });
    expect(api).toHaveBeenCalledTimes(2);
  });

  test.each(['writes', 'reads', 'reads and writes'])('tab store preserves in-flight guard across remounts when storage %s fail', async (failure) => {
    const readSpy = failure !== 'writes' ? vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError');
    }) : null;
    const writeSpy = failure !== 'reads' ? vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError');
    }) : null;
    let resolveCall!: (value: unknown) => void;
    vi.mocked(api).mockReturnValueOnce(new Promise(resolve => { resolveCall = resolve; }));

    const completion = vi.fn();
    const useMount = () => {
      const mut = useEpicMutations(epicId);
      useEffect(() => mut.registerCompletion('brief-draft', completion), [mut]);
      return mut;
    };

    const first = renderHook(useMount);
    let submitPromise!: Promise<unknown>;
    act(() => {
      submitPromise = first.result.current.execute('PATCH', `/epics/${epicId}`, { title: 'New' }, { kind: 'brief-draft' });
    });

    expect(first.result.current.loading).toBe(true);
    expect(first.result.current.reloadProtected).toBe(false);

    first.unmount();

    const second = renderHook(useMount);
    expect(second.result.current.loading).toBe(true);
    expect(second.result.current.hasPendingRetry).toBe(true);

    await act(async () => {
      await expect(second.result.current.execute('POST', `/epics/${epicId}/brief-revisions`, {})).rejects.toThrow(/already in progress/);
      await expect(second.result.current.retryPending()).rejects.toThrow(/already in progress/);
    });
    expect(api).toHaveBeenCalledTimes(1);

    const receipt = { version: 3 };
    await act(async () => {
      resolveCall(receipt);
      await submitPromise;
    });

    expect(second.result.current.loading).toBe(false);
    expect(second.result.current.hasPendingRetry).toBe(false);
    expect(completion).toHaveBeenCalledTimes(1);
    expect(completion).toHaveBeenCalledWith(receipt, expect.objectContaining({ kind: 'brief-draft' }));
    writeSpy?.mockRestore();
    readSpy?.mockRestore();
  });

  test('a late receipt with no mounted owner and blocked storage remains recoverable within the tab', async () => {
    const read = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('SecurityError'); });
    const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('QuotaExceededError'); });
    try {
      let finish!: (value: unknown) => void;
      vi.mocked(api).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
      const previous = vi.fn();
      const first = renderHook(() => useEpicMutations(epicId));
      const unregisterPrevious = first.result.current.registerCompletion('execution-start', previous);
      let submitted!: Promise<unknown>;
      act(() => { submitted = first.result.current.execute('POST', `/epics/${epicId}/executions`, { expected_epic_version: 1 }, { kind: 'execution-start' }); });
      const original = vi.mocked(api).mock.calls[0];
      first.unmount();
      const receipt = { execution_id: 'late-execution', execution_version: 1 };
      await act(async () => { finish(receipt); await submitted; });
      expect(previous).not.toHaveBeenCalled();
      unregisterPrevious();

      const next = renderHook(() => useEpicMutations(epicId));
      const completed = vi.fn();
      const confirmed = vi.fn();
      next.result.current.registerCompletion('execution-start', completed);
      next.result.current.registerCompletion('*', confirmed);
      expect(next.result.current.hasPendingRetry).toBe(true);
      expect(next.result.current.loading).toBe(false);
      expect(next.result.current.reloadProtected).toBe(false);
      vi.mocked(api).mockResolvedValueOnce(receipt);
      await act(async () => { await next.result.current.retryPending(); });
      expect(vi.mocked(api).mock.calls[1]).toEqual(original);
      expect(completed).toHaveBeenCalledTimes(1);
      expect(confirmed).toHaveBeenCalledTimes(1);
      expect(previous).not.toHaveBeenCalled();
      expect(next.result.current.hasPendingRetry).toBe(false);
    } finally {
      read.mockRestore();
      write.mockRestore();
    }
  });

  test.each([false, true])('a transition reply ignores departed consumers before passive cleanup (live peer: %s)', async (hasPeer) => {
    let finish!: (value: unknown) => void;
    vi.mocked(api).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const receipt = { execution_id: 'transition-execution', execution_version: 1 };
    const departed = vi.fn();
    let passiveAtPeerReceipt: boolean | undefined;
    const peerCompleted = vi.fn(() => { passiveAtPeerReceipt = passiveCleaned; });
    const peerConfirmed = vi.fn();
    let owner!: ReturnType<typeof useEpicMutations>;
    let passiveCleaned = false;
    const settleDuringLayoutCleanup = () => {
      finish(receipt);
      // A slow commit makes Scheduler yield before the passive cleanup task.
      const deadline = performance.now() + 25;
      while (performance.now() < deadline) { /* preserve the real transition ordering */ }
    };
    function PreviousPage() {
      const mutations = useEpicMutations(epicId);
      const { registerCompletion } = mutations;
      useLayoutEffect(() => { owner = mutations; }, [mutations]);
      useLayoutEffect(() => settleDuringLayoutCleanup, []);
      useEffect(() => {
        const unregister = registerCompletion('execution-start', departed);
        return () => { passiveCleaned = true; unregister(); };
      }, [registerCompletion]);
      return <p>Previous epic page</p>;
    }
    function Peer() {
      const { registerCompletion } = useEpicMutations(epicId);
      useEffect(() => {
        const complete = registerCompletion('execution-start', peerCompleted);
        const confirm = registerCompletion('*', peerConfirmed);
        return () => { complete(); confirm(); };
      }, [registerCompletion]);
      return <p>Current epic observer</p>;
    }
    const renderPage = (previous: boolean) => hasPeer
      ? <EpicMutationProvider epicId={epicId}>{previous ? <PreviousPage /> : <p>Child gate page</p>}<Peer /></EpicMutationProvider>
      : previous ? <PreviousPage /> : <p>Child gate page</p>;
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    const actEnvironment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
    const originalActEnvironment = actEnvironment.IS_REACT_ACT_ENVIRONMENT;
    try {
      await act(async () => { root.render(renderPage(true)); });
      let submitted!: Promise<unknown>;
      act(() => {
        submitted = owner.execute('POST', `/epics/${epicId}/executions`, { expected_epic_version: 1 }, { kind: 'execution-start' });
      });
      const original = vi.mocked(api).mock.calls[0];
      // act would flush passive cleanup immediately and hide the real gap.
      actEnvironment.IS_REACT_ACT_ENVIRONMENT = false;
      startTransition(() => { root.render(renderPage(false)); });
      await submitted;
      if (!hasPeer) expect(passiveCleaned).toBe(false);
      expect(departed).not.toHaveBeenCalled();
      if (hasPeer) {
        expect(passiveAtPeerReceipt).toBe(false);
        expect(peerCompleted).toHaveBeenCalledTimes(1);
        expect(peerCompleted).toHaveBeenCalledWith(receipt, expect.objectContaining({ kind: 'execution-start' }));
        expect(peerConfirmed).toHaveBeenCalledTimes(1);
        expect(sessionStorage.getItem(`epic_pending_mutation_${epicId}`)).toBeNull();
        return;
      }
      expect(sessionStorage.getItem(`epic_pending_mutation_${epicId}`)).not.toBeNull();

      actEnvironment.IS_REACT_ACT_ENVIRONMENT = originalActEnvironment;
      const current = renderHook(() => useEpicMutations(epicId));
      const completed = vi.fn();
      current.result.current.registerCompletion('execution-start', completed);
      expect(current.result.current.hasPendingRetry).toBe(true);
      vi.mocked(api).mockResolvedValueOnce(receipt);
      await act(async () => { await current.result.current.retryPending(); });
      expect(vi.mocked(api).mock.calls[1]).toEqual(original);
      expect(completed).toHaveBeenCalledTimes(1);
      expect(completed).toHaveBeenCalledWith(receipt, expect.objectContaining({ kind: 'execution-start' }));
      expect(departed).not.toHaveBeenCalled();
    } finally {
      actEnvironment.IS_REACT_ACT_ENVIRONMENT = originalActEnvironment;
      await act(async () => { root.unmount(); });
      container.remove();
    }
  });

  test('different epics can mutate independently without global in-flight blocking', async () => {
    const epic1 = '11111111-1111-4111-8111-111111111111';
    const epic2 = '22222222-2222-4222-8222-222222222222';

    let finish1!: (val: unknown) => void;
    vi.mocked(api).mockReturnValueOnce(new Promise(resolve => { finish1 = resolve; }))
      .mockResolvedValueOnce({ version: 2 });

    const hook1 = renderHook(() => useEpicMutations(epic1));
    const hook2 = renderHook(() => useEpicMutations(epic2));

    let promise1!: Promise<unknown>;
    act(() => {
      promise1 = hook1.result.current.execute('PATCH', `/epics/${epic1}`, { title: 'E1' });
    });

    expect(hook1.result.current.loading).toBe(true);
    expect(hook2.result.current.loading).toBe(false);

    await act(async () => {
      await hook2.result.current.execute('PATCH', `/epics/${epic2}`, { title: 'E2' });
    });

    expect(hook2.result.current.loading).toBe(false);
    expect(hook1.result.current.loading).toBe(true);

    await act(async () => {
      finish1({ version: 2 });
      await promise1;
    });
    expect(hook1.result.current.loading).toBe(false);
  });

  test('real renderToString server pass receives initial snapshot without observing browser state', async () => {
    const { renderToString } = await import('react-dom/server');
    const browserHook = renderHook(() => useEpicMutations(epicId));
    vi.mocked(api).mockRejectedValueOnce(new Error('lost'));
    await act(async () => {
      await expect(browserHook.result.current.execute('PATCH', `/epics/${epicId}`, { title: 'Browser' })).rejects.toThrow();
    });
    expect(browserHook.result.current.hasPendingRetry).toBe(true);

    const originalWindow = globalThis.window;
    let ssrOutput = '';
    try {
      // @ts-expect-error - testing SSR window isolation
      delete globalThis.window;
      function TestServerComponent() {
        const mutations = useEpicMutations(epicId);
        return <div>{mutations.hasPendingRetry ? 'has-retry' : 'clean-server'}</div>;
      }
      ssrOutput = renderToString(<TestServerComponent />);
    } finally {
      globalThis.window = originalWindow;
    }
    expect(ssrOutput).toContain('clean-server');
  });

  test('uppercase UUID route and lowercase server ID share one mutation scope and preserve frozen path', async () => {
    const uppercaseId = 'AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE';
    const lowercaseId = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';

    const hookUpper = renderHook(() => useEpicMutations(uppercaseId));
    const hookLower = renderHook(() => useEpicMutations(lowercaseId));

    vi.mocked(api).mockRejectedValueOnce(new Error('timeout'));
    await act(async () => {
      await expect(hookUpper.result.current.execute('POST', `/epics/${uppercaseId}/brief-revisions`, { text: 'content' }, { kind: 'brief-revision' })).rejects.toThrow();
    });

    expect(hookUpper.result.current.hasPendingRetry).toBe(true);
    expect(hookLower.result.current.hasPendingRetry).toBe(true);
    expect(hookLower.result.current.pendingMutation?.path).toBe(`/epics/${uppercaseId}/brief-revisions`);

    await act(async () => {
      await expect(hookLower.result.current.execute('PATCH', `/epics/${lowercaseId}`, {})).rejects.toThrow(/competing mutation/i);
    });
  });

  test('action in scope A does not rerender or notify independent scope B', async () => {
    const epicA = '11111111-1111-4111-8111-111111111111';
    const epicB = '22222222-2222-4222-8222-222222222222';

    let rendersB = 0;
    const hookA = renderHook(() => useEpicMutations(epicA));
    const hookB = renderHook(() => {
      rendersB++;
      return useEpicMutations(epicB);
    });

    const snapshotBBeforeMutation = hookB.result.current;
    const rendersBBeforeMutation = rendersB;

    vi.mocked(api).mockResolvedValueOnce({ version: 2 });

    await act(async () => {
      await hookA.result.current.execute('PATCH', `/epics/${epicA}`, { title: 'Scope A update' });
    });

    expect(rendersB).toBe(rendersBBeforeMutation);
    expect(hookB.result.current).toBe(snapshotBBeforeMutation);
  });

  test('supports PUT mutations and formats budget version conflict message', async () => {
    vi.mocked(api).mockRejectedValueOnce(new ApiError(409, 'stale-projection'));

    const { result } = renderHook(() => useEpicMutations(epicId));

    await act(async () => {
      try {
        await result.current.execute('PUT', `/epics/${epicId}/budget`, {
          expected_version: 1,
          ceiling: { billing_mode: 'allowance_only', max_duration_seconds: 1800 },
          disabled_dimensions: [],
        }, { kind: 'budget-edit' });
      } catch {
        // Expected
      }
    });

    expect(result.current.conflict).toBe(true);
    expect(result.current.error).toBe('Budget version conflict: The budget version changed on the server before saving.');
    expect(api).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api).mock.calls[0][1]?.method).toBe('PUT');
  });

  test('tracks executionId on work-item-launch and command mutations and clears it on clearError', async () => {
    const executionId = '22222222-2222-4222-8222-222222222222';
    vi.mocked(api).mockRejectedValueOnce(new ApiError(409, 'epic_launch_blocked', {}, { code: 'epic_launch_blocked', blocker_codes: ['item_deferred'], actual_epic_version: 7, owner_action: 'retry_with_owner_override' }));

    const { result } = renderHook(() => useEpicMutations(epicId));

    await act(async () => {
      await expect(result.current.execute('POST', `/epics/${epicId}/work-item-runs`, {
        schema_version: 1,
        execution_id: executionId,
        item_id: 'item-1',
      }, { kind: 'work-item-launch' })).rejects.toThrow();
    });

    expect(result.current.executionId).toBe(executionId);
    expect(result.current.errorDetail?.blocker_codes).toEqual(['item_deferred']);

    act(() => {
      result.current.clearError();
    });

    expect(result.current.executionId).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.errorDetail).toBeNull();
  });

  test('extracts executionId from command path and restores from sessionStorage', async () => {
    const executionId = '22222222-2222-4222-8222-222222222222';
    const rawPending = {
      method: 'POST',
      path: `/epics/${epicId}/executions/${executionId}/commands`,
      body: { action: 'pause' },
      idempotencyKey: 'saved-cmd-key',
      timestamp: Date.now(),
      kind: 'execution-command',
    };
    sessionStorage.setItem(`epic_pending_mutation_${epicId}`, JSON.stringify(rawPending));

    const { result } = renderHook(() => useEpicMutations(epicId));
    expect(result.current.hasPendingRetry).toBe(true);
    expect(result.current.executionId).toBe(executionId);
  });
});
