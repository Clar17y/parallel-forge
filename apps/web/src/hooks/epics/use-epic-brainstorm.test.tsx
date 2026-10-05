import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { renderHook, act, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { useEpicBrainstorm } from './use-epic-brainstorm';
import { api, ApiError } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) { super(code); }
  },
}));

describe('useEpicBrainstorm', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';
  const projectId = '22222222-2222-4222-8222-222222222222';
  const proposal = {
    schema_version: 1 as const,
    turn_id: '33333333-3333-4333-8333-333333333333',
    problem: 'Problem statement',
    outcomes: ['Outcome'],
    scope: ['Included scope'],
    exclusions: ['Excluded scope'],
    requirements: ['Requirement'],
    requirement_criteria: { Requirement: ['Criterion'] },
    decisions: ['Decision'],
    assumptions: ['Assumption'],
    open_questions: ['Question'],
    resolved_turn_ids: [],
    evidence: [],
  };
  const outcome = {
    schema_version: 1,
    job_id: 'current-job',
    job_version: 4,
    state: 'proposed' as const,
    proposal_digest: 'a'.repeat(64),
    proposal,
    adopted_revision_id: null,
    failure: null,
    usage_known: false,
    process_settled: true,
    usage: { schema_version: 1, duration_ms: null, duration_lower_bound_ms: 42, tool_call_count: 2, input_tokens: null, output_tokens: 3, estimated_api_cost_minor: null, unknown_fields: ['duration_ms', 'input_tokens', 'estimated_api_cost_minor'] },
    reservation: null,
    cumulative_usage: { schema_version: 1, duration_ms: 42, tool_call_count: 2, input_tokens: 0, output_tokens: 3, estimated_api_cost_minor: 0 },
    held_reservations: { schema_version: 1, duration_ms: 0, tool_call_count: 0, input_tokens: 0, output_tokens: 0, estimated_api_cost_minor: 0 },
    uncertain_attempts: 0,
    currency: 'USD',
    unknown_usage_fields: ['duration_ms', 'input_tokens', 'estimated_api_cost_minor'],
    held_reasons: { schema_version: 1, duration_ms: null, tool_call_count: null, input_tokens: null, output_tokens: null, estimated_api_cost_minor: null },
  };

  beforeEach(() => { resetEpicMutationStoreForTesting();
    vi.clearAllMocks();
    sessionStorage.clear();
    window.history.replaceState({}, '', `/epics/${epicId}`);
  });
  afterEach(() => sessionStorage.clear());

  test('keeps authoring unavailable on producer 503', async () => {
    vi.mocked(api).mockRejectedValueOnce(new ApiError(503, 'epic brainstorming unavailable'));
    const { result } = renderHook(() => useEpicBrainstorm(epicId, projectId));
    await waitFor(() => expect(result.current.isUnavailable).toBe(true));
    expect(result.current.threads).toEqual([]);
  });

  test('observes the newest job and exposes its producer version and digest for adoption', async () => {
    const thread = { conversation_id: '44444444-4444-4444-8444-444444444444', conversation_version: 6, job_ids: ['old-job', 'current-job'] };
    const turns = [
      { schema_version: 1, turn_id: '55555555-5555-4555-8555-555555555555', conversation_id: thread.conversation_id, role: 'operator', text: 'Draft requirements', pending: false, proposal: null },
      { schema_version: 1, turn_id: proposal.turn_id, conversation_id: thread.conversation_id, role: 'assistant', text: 'Here is a proposal', pending: false, proposal },
    ];
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${thread.conversation_id}/turns?project_id=${projectId}`) return turns as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/current-job?project_id=${projectId}`) return outcome as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/current-job/adopt` && init?.method === 'POST') return { brief_revision_id: '66666666-6666-4666-8666-666666666666' } as T;
      return undefined as T;
    });
    const { result } = renderHook(() => useEpicBrainstorm(epicId, projectId));
    await waitFor(() => expect(result.current.outcome?.job_version).toBe(4));

    expect(result.current.selectedJobId).toBe('current-job');
    expect(result.current.outcome?.proposal_digest).toBe('a'.repeat(64));
    await act(async () => {
      await result.current.adoptProposal('current-job', result.current.outcome!.proposal_digest!, result.current.outcome!.job_version, 9);
    });
    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-jobs/current-job/adopt`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ schema_version: 1, project_id: projectId, expected_job_version: 4, expected_epic_version: 9, proposal_digest: 'a'.repeat(64) }),
    }));
  });

  test('submits the saved operator turn against the current conversation and epic versions', async () => {
    const thread = { conversation_id: '77777777-7777-4777-8777-777777777777', conversation_version: 3, job_ids: [] };
    const operator = { schema_version: 1, turn_id: '88888888-8888-4888-8888-888888888888', conversation_id: thread.conversation_id, role: 'operator', text: 'Draft this', pending: false, proposal: null };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${thread.conversation_id}/turns?project_id=${projectId}`) return [operator] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${thread.conversation_id}/jobs` && init?.method === 'POST') return { schema_version: 1, job_id: 'new-job', job_version: 1, state: 'queued', replay_key: 'key' } as T;
      return undefined as T;
    });
    const { result } = renderHook(() => useEpicBrainstorm(epicId, projectId));
    await waitFor(() => expect(result.current.turns).toHaveLength(1));
    await act(async () => { await result.current.submitJob(thread.conversation_id, operator.turn_id, 12, thread.conversation_version); });
    expect(result.current.selectedJobId).toBe('new-job');

    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-conversations/${thread.conversation_id}/jobs`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ schema_version: 1, project_id: projectId, prompt_turn_id: operator.turn_id, expected_epic_version: 12, expected_conversation_version: 3 }),
    }));
  });

  test('reconciles a URL job to its actual conversation after thread ownership loads', async () => {
    const threadA = { conversation_id: 'thread-A', conversation_version: 3, job_ids: ['job-A'] };
    const threadB = { conversation_id: 'thread-B', conversation_version: 4, job_ids: ['job-B'] };
    let resolveThreads!: (threads: typeof threadA[]) => void;
    window.history.replaceState({}, '', `/epics/${epicId}?conversation_id=thread-B&job_id=job-A`);
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) {
        return await new Promise<typeof threadA[]>(resolve => { resolveThreads = resolve; }) as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/thread-A/turns?project_id=${projectId}`) return [] as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/job-A?project_id=${projectId}`) return { ...outcome, job_id: 'job-A' } as T;
      return undefined as T;
    });

    const { result } = renderHook(() => useEpicBrainstorm(epicId, projectId));
    expect(result.current.activeConversationId).toBe('thread-B');
    expect(result.current.selectedJobId).toBeNull();
    await act(async () => { resolveThreads([threadB, threadA]); });
    await waitFor(() => expect(result.current.activeConversationId).toBe('thread-A'));
    await waitFor(() => expect(result.current.outcome?.job_id).toBe('job-A'));
    expect(result.current.selectedJobId).toBe('job-A');
    expect(new URL(window.location.href).searchParams.get('conversation_id')).toBe('thread-A');
    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-conversations/thread-A/turns?project_id=${projectId}`, expect.any(Object));
    expect(result.current.activeConversationId).not.toBe('thread-B');
  });
});
