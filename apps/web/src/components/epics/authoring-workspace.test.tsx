import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderToString } from 'react-dom/server';
import { hydrateRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { AuthoringWorkspace } from './authoring-workspace';
import { api } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) { super(code); }
  },
}));

describe('AuthoringWorkspace', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';
  const projectId = '22222222-2222-4222-8222-222222222222';
  const conversationId = '33333333-3333-4333-8333-333333333333';
  const proposal = {
    schema_version: 1,
    turn_id: '55555555-5555-4555-8555-555555555555',
    problem: 'Authentication is difficult to audit.',
    outcomes: ['Operators can trace login decisions.'],
    scope: ['Session token validation'],
    exclusions: ['Password reset'],
    requirements: ['Tokens expire'],
    requirement_criteria: { 'Tokens expire': ['Expired tokens are rejected.'] },
    decisions: ['Use short lived access tokens.'],
    assumptions: ['Clients can refresh credentials.'],
    open_questions: ['What is the default lifetime?'],
    resolved_turn_ids: [],
    evidence: [{ schema_version: 1, path: 'docs/auth.md', content_digest: 'b'.repeat(64), excerpt: 'The current session policy.' }],
  };
  const thread = { conversation_id: conversationId, conversation_version: 4, job_ids: ['old-job', 'current-job'] };
  const turns = [
    { schema_version: 1, turn_id: '44444444-4444-4444-8444-444444444444', conversation_id: conversationId, role: 'operator', text: 'Draft a secure auth brief.', pending: false, proposal: null },
    { schema_version: 1, turn_id: proposal.turn_id, conversation_id: conversationId, role: 'assistant', text: 'Here is a proposal.', pending: false, proposal },
  ];
  const proposedOutcome = {
    schema_version: 1,
    job_id: 'current-job',
    job_version: 7,
    state: 'proposed',
    proposal_digest: 'a'.repeat(64),
    proposal,
    adopted_revision_id: null,
    failure: null,
    usage_known: false,
    process_settled: true,
    usage: { schema_version: 1, duration_ms: null, duration_lower_bound_ms: 300, tool_call_count: 2, input_tokens: null, output_tokens: 50, estimated_api_cost_minor: null, unknown_fields: ['duration_ms', 'input_tokens', 'estimated_api_cost_minor'] },
    reservation: null,
    cumulative_usage: { schema_version: 1, duration_ms: 300, tool_call_count: 2, input_tokens: 0, output_tokens: 50, estimated_api_cost_minor: 0 },
    held_reservations: { schema_version: 1, duration_ms: 0, tool_call_count: 0, input_tokens: 0, output_tokens: 0, estimated_api_cost_minor: 0 },
    uncertain_attempts: 0,
    currency: 'USD',
    unknown_usage_fields: ['duration_ms', 'input_tokens', 'estimated_api_cost_minor'],
    held_reasons: { schema_version: 1, duration_ms: null, tool_call_count: null, input_tokens: null, output_tokens: null, estimated_api_cost_minor: null },
  };

  function mockAuthoring(outcome = proposedOutcome) {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) return turns as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/current-job?project_id=${projectId}`) return outcome as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/current-job/adopt` && init?.method === 'POST') return { brief_revision_id: '66666666-6666-4666-8666-666666666666' } as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs` && init?.method === 'POST') return { schema_version: 1, job_id: 'new-job', job_version: 1, state: 'queued', replay_key: 'key' } as T;
      return undefined as T;
    });
  }

  beforeEach(() => { resetEpicMutationStoreForTesting(); vi.clearAllMocks(); sessionStorage.clear(); window.history.replaceState({}, '', `/epics/${epicId}`); });
  afterEach(() => { resetEpicMutationStoreForTesting(); cleanup(); vi.mocked(api).mockReset(); sessionStorage.clear(); });

  test('URL conversation selection restores without a server/client hydration mismatch', async () => {
    window.history.replaceState({}, '', `/epics/${epicId}?conversation_id=${conversationId}`);
    vi.mocked(api).mockImplementation(() => new Promise(() => {}));
    const browserWindow = window;
    vi.stubGlobal('window', undefined);
    let serverMarkup: string;
    try {
      serverMarkup = renderToString(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    } finally {
      vi.stubGlobal('window', browserWindow);
    }
    const container = document.createElement('div');
    document.body.append(container);
    container.innerHTML = serverMarkup;
    const recoverableError = vi.fn();
    const root = hydrateRoot(container, <AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />, { onRecoverableError: recoverableError });
    try {
      await waitFor(() => expect(container.querySelector('button[type="submit"]')).toHaveTextContent('Send message'));
      expect(recoverableError).not.toHaveBeenCalled();
    } finally {
      await act(async () => { root.unmount(); });
      container.remove();
      vi.unstubAllGlobals();
    }
  });

  test('renders the full proposed brief separately and adopts with the current outcome identity', async () => {
    mockAuthoring();
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);

    expect(await screen.findByText('Authentication is difficult to audit.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Proposed brief', level: 3 })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Problem', level: 4 })).toBeInTheDocument();
    for (const text of ['Operators can trace login decisions.', 'Session token validation', 'Password reset', 'Expired tokens are rejected.', 'Use short lived access tokens.', 'Clients can refresh credentials.', 'What is the default lifetime?']) {
      expect(screen.getByText(text)).toBeInTheDocument();
    }
    expect(screen.getByText('Supporting evidence')).toBeInTheDocument();
    expect(screen.getByText('Proposal')).toBeInTheDocument();
    expect(screen.getByText(/Usage: unknown/)).toBeInTheDocument();
    expect(screen.getByText(/Unknown usage: duration ms, input tokens, estimated api cost minor/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Adopt proposed brief' }));
    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-jobs/current-job/adopt`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ schema_version: 1, project_id: projectId, expected_job_version: 7, expected_epic_version: 9, proposal_digest: 'a'.repeat(64) }),
    }));
  });

  test('submits the latest saved operator message', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [{ ...thread, job_ids: [] }] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) return turns as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs` && init?.method === 'POST') return { schema_version: 1, job_id: 'new-job', job_version: 1, state: 'queued', replay_key: 'key' } as T;
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await screen.findByText('Here is a proposal.');

    await userEvent.click(screen.getByRole('button', { name: 'Generate proposed brief' }));
    await waitFor(() => expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ schema_version: 1, project_id: projectId, prompt_turn_id: turns[0].turn_id, expected_epic_version: 9, expected_conversation_version: 4 }),
    })));
    expect(screen.queryByText(/Job state: quota wait/i)).not.toBeInTheDocument();
  });

  test('requests cancellation using the observed job version and distinguishes process settlement', async () => {
    let observedOutcome = { ...proposedOutcome, state: 'quota_wait', proposal: null, proposal_digest: null, usage_known: null, usage: null, process_settled: false, unknown_usage_fields: [] };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) return turns as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/current-job?project_id=${projectId}`) return observedOutcome as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/current-job/cancel` && init?.method === 'POST') {
        observedOutcome = { ...observedOutcome, state: 'cancel_requested', job_version: 8 };
        return { schema_version: 1, job_id: 'current-job', job_version: 8, state: 'cancel_requested', replay_key: 'key' } as T;
      }
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    expect(await screen.findByText(/Job state: quota wait/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel assistant job' }));
    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-jobs/current-job/cancel`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ schema_version: 1, project_id: projectId, expected_job_version: 7 }),
    }));
    await waitFor(() => expect(screen.getByText(/Cancellation requested/)).toBeInTheDocument());
    expect(screen.getByText(/Process settlement pending/)).toBeInTheDocument();
  });

  test('offers retry only after a failed job process has settled', async () => {
    const failed = { ...proposedOutcome, state: 'failed', proposal: null, proposal_digest: null, failure: 'quota_exhausted', process_settled: true };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) return turns as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/current-job?project_id=${projectId}`) return failed as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/current-job/retry` && init?.method === 'POST') return { schema_version: 1, job_id: 'current-job', job_version: 8, state: 'queued', replay_key: 'key' } as T;
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry assistant job' }));
    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-jobs/current-job/retry`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ schema_version: 1, project_id: projectId, expected_job_version: 7 }),
    }));
  });

  test('keeps a restored request retry available when authoring reads are unavailable', async () => {
    const pending = {
      kind: 'conversation-start',
      method: 'POST',
      path: `/epics/${epicId}/brainstorm-conversations`,
      body: { schema_version: 1, project_id: projectId, text: 'Begin a conversation' },
      idempotencyKey: 'original-request-key',
      timestamp: 123,
    };
    sessionStorage.setItem(`epic_pending_mutation_${epicId}`, JSON.stringify(pending));
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) throw new Error('authoring route unavailable');
      if (path === pending.path && init?.method === 'POST') throw new Error('connection lost');
      return [] as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={1} />);
    expect(await screen.findByText('Retry original request')).toBeInTheDocument();
    expect(screen.getByText('Authoring service unavailable')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Retry original request' }));
    expect(api).toHaveBeenCalledWith(pending.path, expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'Idempotency-Key': pending.idempotencyKey }),
      body: JSON.stringify(pending.body),
    }));
  });

  test('replays a pending job submission for thread A after thread B becomes the default', async () => {
    const threadA = { conversation_id: 'thread-A', conversation_version: 5, job_ids: [] as string[] };
    const threadB = { conversation_id: 'thread-B', conversation_version: 2, job_ids: ['job-B'] };
    const body = { schema_version: 1, project_id: projectId, prompt_turn_id: 'prompt-A', expected_epic_version: 9, expected_conversation_version: 5 };
    sessionStorage.setItem(`epic_pending_mutation_${epicId}`, JSON.stringify({
      kind: 'job-submit', method: 'POST', path: `/epics/${epicId}/brainstorm-conversations/thread-A/jobs`, body,
      idempotencyKey: 'job-submit-A-key', timestamp: 123, uncertain: true,
    }));
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [threadB, threadA] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/thread-B/turns?project_id=${projectId}` || path === `/epics/${epicId}/brainstorm-conversations/thread-A/turns?project_id=${projectId}`) return [] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/thread-A/jobs` && init?.method === 'POST') return { schema_version: 1, job_id: 'job-A', job_version: 1, state: 'queued', replay_key: 'receipt' } as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/job-A?project_id=${projectId}`) return { ...proposedOutcome, job_id: 'job-A', state: 'failed', proposal: null, proposal_digest: null, failure: 'timeout', usage: null, usage_known: false, unknown_usage_fields: ['duration_ms'], process_settled: false } as T;
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry original request' }));
    await screen.findByText('The job reported: timeout.');
    expect(screen.getByLabelText('Conversation')).toHaveValue('thread-A');
    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-conversations/thread-A/jobs`, expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'Idempotency-Key': 'job-submit-A-key' }),
      body: JSON.stringify(body),
    }));
  });

  test('clears a whitespace-padded prompt through the same completion path after replay', async () => {
    let startCalls = 0;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations` && init?.method === 'POST') {
        startCalls += 1;
        if (startCalls === 1) throw new Error('connection lost');
        return { conversation_id: 'created-conversation', version: 1 } as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/created-conversation/turns?project_id=${projectId}`) return [] as T;
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    const input = screen.getByLabelText('Send message or instruction');
    await userEvent.type(input, '  Begin with a clear brief.  ');
    await userEvent.click(screen.getByRole('button', { name: 'Start conversation' }));
    await screen.findByRole('button', { name: 'Retry original request' });
    expect(input).toHaveValue('  Begin with a clear brief.  ');

    await userEvent.click(screen.getByRole('button', { name: 'Retry original request' }));
    await waitFor(() => expect(new URL(window.location.href).searchParams.get('conversation_id')).toBe('created-conversation'));
    expect(input).toHaveValue('');
    const replay = vi.mocked(api).mock.calls.filter(([path, init]) => path === `/epics/${epicId}/brainstorm-conversations` && init?.method === 'POST');
    expect(replay).toHaveLength(2);
    expect(replay[0][1]).toEqual(expect.objectContaining({ body: JSON.stringify({ schema_version: 1, project_id: projectId, text: 'Begin with a clear brief.' }) }));
    expect(replay[1][1]).toEqual(expect.objectContaining({
      headers: expect.objectContaining({ 'Idempotency-Key': replay[0][1]?.headers && new Headers(replay[0][1].headers).get('Idempotency-Key') }),
      body: JSON.stringify({ schema_version: 1, project_id: projectId, text: 'Begin with a clear brief.' }),
    }));
  });

  test('lets the operator inspect a prior job outcome without changing the newest job', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) return turns as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/current-job?project_id=${projectId}`) return proposedOutcome as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/old-job?project_id=${projectId}`) return { ...proposedOutcome, job_id: 'old-job', state: 'failed', proposal: null, proposal_digest: null, failure: 'unavailable', usage: null, usage_known: false, unknown_usage_fields: ['duration_ms'], process_settled: false } as T;
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    expect(await screen.findByText(/Job state: proposed/i)).toBeInTheDocument();
    const jobSelector = screen.getByRole('combobox', { name: 'Assistant job' });
    expect(jobSelector).toHaveClass('block', 'w-full');
    expect(jobSelector.closest('div')).toHaveClass('flex-col');
    await userEvent.selectOptions(jobSelector, 'old-job');
    expect(await screen.findByText(/Job state: failed/i)).toBeInTheDocument();
    expect(screen.getByText('The job reported: unavailable.')).toBeInTheDocument();
  });

  test('reports No process settlement reported for cancelled job with no attempt', async () => {
    const cancelledOutcome = {
      schema_version: 1,
      job_id: 'current-job',
      job_version: 2,
      state: 'cancelled',
      proposal_digest: null,
      proposal: null,
      adopted_revision_id: null,
      failure: null,
      usage_known: null,
      process_settled: false,
      usage: null,
      reservation: null,
      cumulative_usage: { schema_version: 1, duration_ms: 0, tool_call_count: 0, input_tokens: 0, output_tokens: 0, estimated_api_cost_minor: 0 },
      held_reservations: { schema_version: 1, duration_ms: 0, tool_call_count: 0, input_tokens: 0, output_tokens: 0, estimated_api_cost_minor: 0 },
      uncertain_attempts: 0,
      currency: null,
      unknown_usage_fields: [],
      held_reasons: { schema_version: 1, duration_ms: null, tool_call_count: null, input_tokens: null, output_tokens: null, estimated_api_cost_minor: null },
    };
    mockAuthoring(cancelledOutcome as any);
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    expect(await screen.findByText('No process settlement reported')).toBeInTheDocument();
    expect(screen.queryByText('Process settlement pending')).not.toBeInTheDocument();
  });
});
