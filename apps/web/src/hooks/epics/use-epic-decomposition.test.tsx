import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { renderHook, act, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { useEpicDecomposition } from './use-epic-decomposition';
import { api } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) { super(code); }
  },
}));

describe('useEpicDecomposition', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';
  const projectId = '22222222-2222-4222-8222-222222222222';
  const thread = { conversation_id: 'conv-1', conversation_version: 2, job_ids: ['job-1'] };
  const turn = {
    schema_version: 1,
    turn_id: 'turn-1',
    conversation_id: 'conv-1',
    role: 'operator' as const,
    text: 'Decompose work items',
    pending: false,
  };
  const outcome = {
    schema_version: 1,
    job_id: 'job-1',
    job_version: 3,
    state: 'failed' as const,
    proposal_digest: null,
    failure: 'rate limit exceeded',
    usage_known: true,
    process_settled: true,
    usage: null,
    unknown_usage_fields: [],
  };

  beforeEach(() => {
    resetEpicMutationStoreForTesting();
    vi.clearAllMocks();
    sessionStorage.clear();
    window.history.replaceState({}, '', `/epics/${epicId}`);
  });

  afterEach(() => {
    resetEpicMutationStoreForTesting();
    sessionStorage.clear();
  });

  test('loads decomposition conversations and observes job outcome', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path.startsWith(`/epics/${epicId}/decomposition-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/decomposition-conversations/conv-1/turns?project_id=${projectId}`) return [turn] as T;
      if (path === `/epics/${epicId}/decomposition-jobs/job-1?project_id=${projectId}`) return outcome as T;
      return undefined as T;
    });

    const { result } = renderHook(() => useEpicDecomposition(epicId, projectId));
    await waitFor(() => expect(result.current.threads).toHaveLength(1));
    await waitFor(() => expect(result.current.outcome?.job_id).toBe('job-1'));

    expect(result.current.activeConversationId).toBe('conv-1');
    expect(result.current.turns).toHaveLength(1);
    expect(result.current.outcome?.failure).toBe('rate limit exceeded');
  });

  test('retries failed decomposition job with owner_override and override_note', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/decomposition-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/decomposition-jobs/job-1/retry` && init?.method === 'POST') {
        return { schema_version: 1, job_id: 'job-1', job_version: 4, state: 'queued', replay_key: 'rk' } as T;
      }
      return undefined as T;
    });

    const { result } = renderHook(() => useEpicDecomposition(epicId, projectId));

    await act(async () => {
      await result.current.retryJob('job-1', 3, true, 'Owner bypass for decomposition quota');
    });

    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/decomposition-jobs/job-1/retry`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        project_id: projectId,
        expected_job_version: 3,
        owner_override: true,
        override_note: 'Owner bypass for decomposition quota',
      }),
    }));
  });
});
