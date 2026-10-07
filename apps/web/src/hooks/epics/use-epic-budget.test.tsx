import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { renderHook, act, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { useEpicBudget } from './use-epic-budget';
import { api, ApiError } from '@/lib/api/client';
import type { EpicBudgetProjection, TaskBudget } from './types';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) { super(code); }
  },
}));

describe('useEpicBudget', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';
  const defaultCeiling: TaskBudget = {
    billing_mode: 'allowance_only',
    max_duration_seconds: 1800,
    max_cost_minor: 500,
    max_input_tokens: 100000,
    max_output_tokens: 20000,
    max_tool_calls: 50,
    max_provider_attempts: 3,
    max_named_checks: 10,
    max_repairs: 3,
  };

  const sampleBudget: EpicBudgetProjection = {
    epic_id: epicId,
    initialized: true,
    version: 4,
    ceiling: defaultCeiling,
    disabled_dimensions: ['duration_ms'],
    known: {
      tool_call_count: 0,
      input_tokens: 1500,
      output_tokens: 300,
      estimated_api_cost_minor: 12,
      duration_ms: 4500,
      provider_attempts: 1,
    },
    held: {
      tool_call_count: 5,
      input_tokens: 10000,
      output_tokens: 2000,
      estimated_api_cost_minor: 50,
      duration_ms: 0,
      provider_attempts: 0,
    },
    unknown: true,
    currency: 'USD',
    warnings: ['Approaching token ceiling'],
    permits: [
      {
        permit_id: 'p1',
        run_id: 'run-1',
        actor_id: 'act-1',
        consumed_attempt_id: null,
        note: 'Permit for integration run',
        warnings: [],
      },
    ],
    owner_actions: [
      {
        actor_id: 'act-1',
        event_type: 'edit_budget',
        version: 3,
        note: 'Raised token limits',
      },
    ],
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

  test('loads budget projection and distinguishes known zero, unknown, and unlimited dimensions', async () => {
    vi.mocked(api).mockResolvedValue(sampleBudget);

    const { result } = renderHook(() => useEpicBudget(epicId));
    await waitFor(() => expect(result.current.budget).not.toBeNull());

    expect(result.current.budget?.version).toBe(4);
    expect(result.current.isUnlimited('duration_ms')).toBe(true);
    expect(result.current.isUnlimited('tool_call_count')).toBe(false);
    expect(result.current.getKnown('tool_call_count')).toBe(0);
    expect(result.current.getKnown('input_tokens')).toBe(1500);
    expect(result.current.getHeld('tool_call_count')).toBe(5);
    expect(result.current.isUnknown).toBe(true);
    expect(result.current.currency).toBe('USD');
    expect(result.current.warnings).toEqual(['Approaching token ceiling']);
    expect(result.current.permits).toHaveLength(1);
    expect(result.current.ownerActions).toHaveLength(1);
  });

  test('edits budget via PUT /epics/{epicId}/budget with disabled_dimensions and optional note', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) return sampleBudget as T;
      if (path === `/epics/${epicId}/budget` && init?.method === 'PUT') {
        return {
          epic_id: epicId,
          version: 5,
          ceiling: { ...defaultCeiling, max_cost_minor: 1000 },
          disabled_dimensions: ['duration_ms', 'output_tokens'],
        } as T;
      }
      return undefined as T;
    });

    const { result } = renderHook(() => useEpicBudget(epicId));
    await waitFor(() => expect(result.current.budget).not.toBeNull());

    await act(async () => {
      await result.current.editBudget({
        ceiling: { ...defaultCeiling, max_cost_minor: 1000 },
        expectedVersion: 4,
        disabledDimensions: ['duration_ms', 'output_tokens'],
        note: 'Raised cost limit and unset output token cap',
      });
    });

    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/budget`, expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({
        ceiling: { ...defaultCeiling, max_cost_minor: 1000 },
        expected_version: 4,
        disabled_dimensions: ['duration_ms', 'output_tokens'],
        note: 'Raised cost limit and unset output token cap',
      }),
    }));
  });

  test('requests budget admission permit via POST /epics/{epicId}/budget/admissions', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) return sampleBudget as T;
      if (path === `/epics/${epicId}/budget/admissions` && init?.method === 'POST') {
        return {
          permit_id: 'new-permit',
          run_id: 'run-99',
          epic_id: epicId,
          actor_id: 'act-1',
          budget_version: 5,
          warnings: ['Permit admitted beyond soft ceiling'],
        } as T;
      }
      return undefined as T;
    });

    const { result } = renderHook(() => useEpicBudget(epicId));
    await waitFor(() => expect(result.current.budget).not.toBeNull());

    await act(async () => {
      await result.current.permitBudgetAdmission({
        runId: 'run-99',
        expectedVersion: 4,
        note: 'Emergency admission for high priority run',
      });
    });

    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/budget/admissions`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        run_id: 'run-99',
        expected_version: 4,
        note: 'Emergency admission for high priority run',
      }),
    }));
  });

  test('replays uncertain budget edit mutation with exact key and body', async () => {
    let putCalls = 0;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) return sampleBudget as T;
      if (path === `/epics/${epicId}/budget` && init?.method === 'PUT') {
        putCalls++;
        if (putCalls === 1) throw new Error('network down');
        return { epic_id: epicId, version: 5, ceiling: defaultCeiling, disabled_dimensions: [] } as T;
      }
      return undefined as T;
    });

    const { result } = renderHook(() => useEpicBudget(epicId));
    await waitFor(() => expect(result.current.budget).not.toBeNull());

    await act(async () => {
      try {
        await result.current.editBudget({
          ceiling: defaultCeiling,
          expectedVersion: 4,
          disabledDimensions: [],
        });
      } catch {
        // Expected
      }
    });

    expect(result.current.mutations.hasPendingRetry).toBe(true);
    const original = result.current.mutations.pendingMutation;
    expect(original?.method).toBe('PUT');

    await act(async () => {
      await result.current.mutations.retryPending();
    });

    expect(putCalls).toBe(2);
    const putCallsList = vi.mocked(api).mock.calls.filter(([p, init]) => p === `/epics/${epicId}/budget` && init?.method === 'PUT');
    expect(putCallsList).toHaveLength(2);
    const key1 = (putCallsList[0][1]?.headers as Record<string, string>)['Idempotency-Key'];
    const key2 = (putCallsList[1][1]?.headers as Record<string, string>)['Idempotency-Key'];
    expect(key2).toBe(key1);
  });
});
