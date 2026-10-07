import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { renderHook, act } from '@testing-library/react';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import { useEpicWorkItemRuns } from './use-epic-work-item-runs';
import { api } from '@/lib/api/client';
import type { EpicAttemptResponse, EpicLaunchRequest } from './types';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) {
      super(code);
    }
  },
}));

describe('useEpicWorkItemRuns', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';
  const executionId = '22222222-2222-4222-8222-222222222222';
  const itemId = '66666666-6666-4666-8666-666666666666';

  beforeEach(() => {
    resetEpicMutationStoreForTesting();
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  test('does not initiate recurring or initial read-fetch for epic work-item-runs', async () => {
    const { result } = renderHook(() => useEpicWorkItemRuns(epicId));
    await act(async () => { await Promise.resolve(); });

    // Confirm that GET /epics/{epicId}/work-item-runs was NOT called
    expect(api).not.toHaveBeenCalled();
    expect(result.current.launch).toBeDefined();
  });

  test('launch executes POST /epics/{epicId}/work-item-runs with exact payload and idempotency key', async () => {
    const attempt: EpicAttemptResponse = {
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
      item_id: itemId,
      override_note: null,
      owner_override: false,
      run_id: '88888888-8888-4888-8888-888888888888',
      task_digest: 't'.repeat(64),
      task_id: 'task-1',
    };

    vi.mocked(api).mockResolvedValue(attempt);

    const { result } = renderHook(() => useEpicWorkItemRuns(epicId));

    const request: EpicLaunchRequest = {
      schema_version: 1,
      expected_epic_version: 7,
      execution_id: executionId,
      brief_revision_id: '33333333-3333-4333-8333-333333333333',
      brief_digest: 'b'.repeat(64),
      graph_revision_id: '44444444-4444-4444-8444-444444444444',
      graph_digest: 'g'.repeat(64),
      item_id: itemId,
      owner_override: false,
    };

    let response: EpicAttemptResponse | undefined;
    await act(async () => {
      response = await result.current.launch(request);
    });

    expect(response).toEqual(attempt);
    expect(api).toHaveBeenCalledWith(
      `/epics/${epicId}/work-item-runs`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify(request),
        headers: expect.objectContaining({
          'Idempotency-Key': expect.any(String),
        }),
      }),
    );
  });
});
