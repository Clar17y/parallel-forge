import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderToString } from 'react-dom/server';
import { hydrateRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { AuthoringWorkspace, resetSavedBrainstormSendsForTesting } from './authoring-workspace';
import { api, ApiError } from '@/lib/api/client';

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
  const thread = { conversation_id: conversationId, conversation_version: 3, job_ids: ['old-job', 'current-job'] };
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

  beforeEach(() => { resetEpicMutationStoreForTesting(); resetSavedBrainstormSendsForTesting(); vi.clearAllMocks(); sessionStorage.clear(); window.history.replaceState({}, '', `/epics/${epicId}`); });
  afterEach(() => { resetEpicMutationStoreForTesting(); resetSavedBrainstormSendsForTesting(); cleanup(); vi.restoreAllMocks(); vi.mocked(api).mockReset(); sessionStorage.clear(); });

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
    const reviewBrief = vi.fn();
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} onReviewBrief={reviewBrief} />);

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
    await userEvent.click(await screen.findByRole('button', { name: 'Review or edit the adopted brief' }));
    expect(reviewBrief).toHaveBeenCalledOnce();
  });

  test('a follow-up message automatically queues help against its receipt version', async () => {
    const followUp = { ...turns[0], turn_id: 'new-follow-up', text: 'The repairs happen across several depots.' };
    const currentThread = { ...thread, conversation_version: 3, job_ids: ['active-job'] };
    const activeOutcome = { schema_version: 1, job_id: 'active-job', job_version: 2, state: 'running', usage_known: null, process_settled: false, usage: null, unknown_usage_fields: [] };
    let persistedTurns = [turns[0], turns[1], followUp];
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [currentThread] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) return persistedTurns as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/active-job?project_id=${projectId}`) return activeOutcome as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns` && init?.method === 'POST') {
        return { version: 4 } as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs` && init?.method === 'POST') return { schema_version: 1, job_id: 'new-job', job_version: 1, state: 'queued', replay_key: 'key' } as T;
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.type(await screen.findByLabelText('What are you trying to accomplish?'), followUp.text);
    expect(screen.getByRole('status')).toHaveTextContent('The assistant is working on your idea. You can add context while you wait.');
    expect(screen.getByRole('button', { name: 'Cancel assistant job' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Send message' }));
    await waitFor(() => expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ schema_version: 1, project_id: projectId, prompt_turn_id: followUp.turn_id, expected_epic_version: 9, expected_conversation_version: 4 }),
    })));
    expect(screen.queryByRole('button', { name: 'Generate proposed brief' })).not.toBeInTheDocument();
    expect(screen.queryByText(/Job state: quota wait/i)).not.toBeInTheDocument();
  });

  test('sending a rough idea queues assistance for the saved turn automatically', async () => {
    const savedTurn = { ...turns[0], turn_id: 'saved-rough-idea', text: 'I need a way to track field repairs.' };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations` && init?.method === 'POST') return { conversation_id: conversationId, version: 2 } as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) return [savedTurn] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs` && init?.method === 'POST') return { schema_version: 1, job_id: 'rough-idea-job', job_version: 1, state: 'queued', replay_key: 'key' } as T;
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.type(screen.getByLabelText('What are you trying to accomplish?'), 'I need a way to track field repairs.');
    await userEvent.click(screen.getByRole('button', { name: 'Start conversation' }));
    await waitFor(() => expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ schema_version: 1, project_id: projectId, prompt_turn_id: 'saved-rough-idea', expected_epic_version: 9, expected_conversation_version: 2 }),
    })));
    expect(screen.queryByRole('button', { name: 'Generate proposed brief' })).not.toBeInTheDocument();
  });

  test('recovers a saved message after remount without saving it twice', async () => {
    let startCount = 0;
    let jobCount = 0;
    let directReads = 0;
    const savedTurn = { ...turns[0], turn_id: 'recoverable-turn', text: 'Track repairs by depot.' };
    sessionStorage.setItem(`epic_saved_brainstorm_send_${epicId}`, JSON.stringify({ conversationId, version: 2 }));
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations` && init?.method === 'POST') {
        startCount += 1;
        return { conversation_id: conversationId, version: 2 } as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) {
        if (!init?.signal) {
          directReads += 1;
          if (directReads === 1) throw new Error('read failed after save');
        }
        return [savedTurn] as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs` && init?.method === 'POST') {
        jobCount += 1;
        return { schema_version: 1, job_id: 'recovered-job', job_version: 1, state: 'queued', replay_key: 'key' } as T;
      }
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry assistant help' }));
    expect(await screen.findByText(/Your message is saved\./)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Retry assistant help' }));
    await waitFor(() => expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ schema_version: 1, project_id: projectId, prompt_turn_id: savedTurn.turn_id, expected_epic_version: 9, expected_conversation_version: 2 }),
    })));
    expect(startCount).toBe(0);
    expect(jobCount).toBe(1);
    expect(directReads).toBe(2);
  });

  test('uses the current epic version when an unsubmitted saved turn is recovered after a failed read', async () => {
    const selectedRoute = { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' };
    const savedTurn = { ...turns[0], turn_id: 'saved-current-version' };
    const receipts = new Map<string, { job_id: string; body: string }>();
    let reads = 0;
    sessionStorage.setItem(`epic_saved_brainstorm_send_${epicId}`, JSON.stringify({ conversationId, version: 2, expectedEpicVersion: 9, route: selectedRoute }));
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [] as T;
      if (path.endsWith(`/turns?project_id=${projectId}`)) {
        if (!init?.signal && ++reads === 1) throw new Error('read unavailable');
        return [savedTurn] as T;
      }
      if (path.endsWith('/jobs') && init?.method === 'POST') {
        const body = init.body as string;
        const key = new Headers(init.headers).get('Idempotency-Key')!;
        const replay = receipts.get(key);
        if (replay) { if (replay.body !== body) throw new Error('replay body changed'); return { job_id: replay.job_id } as T; }
        if (JSON.parse(body).expected_epic_version !== 10) throw new ApiError(409, 'conflict');
        receipts.set(key, { job_id: 'current-job', body });
        return { job_id: 'current-job' } as T;
      }
      return undefined as T;
    });
    const view = render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry assistant help' }));
    await screen.findByText(/could not start the assistant/);
    view.rerender(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={10} />);
    await userEvent.click(screen.getByRole('button', { name: 'Retry assistant help' }));
    await waitFor(() => expect(receipts.size).toBe(1));
    expect(JSON.parse([...receipts.values()][0].body).expected_epic_version).toBe(10);
    expect(JSON.parse([...receipts.values()][0].body).requested_route).toEqual(selectedRoute);
  });

  test('freezes the current version after a pending saved-turn read resolves', async () => {
    const savedTurn = { ...turns[0], turn_id: 'pending-read-turn' };
    let resolveRead!: (value: typeof turns) => void;
    const versions: number[] = [];
    sessionStorage.setItem(`epic_saved_brainstorm_send_${epicId}`, JSON.stringify({ conversationId, version: 2, expectedEpicVersion: 9 }));
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [] as T;
      if (path.endsWith(`/turns?project_id=${projectId}`)) {
        if (!init?.signal) return await new Promise<typeof turns>(resolve => { resolveRead = resolve; }) as T;
        return [savedTurn] as T;
      }
      if (path.endsWith('/jobs') && init?.method === 'POST') {
        versions.push(JSON.parse(init.body as string).expected_epic_version);
        return { job_id: 'pending-read-job' } as T;
      }
      return undefined as T;
    });
    const view = render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry assistant help' }));
    await screen.findByRole('button', { name: 'Starting assistant…' });
    view.rerender(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={10} />);
    await act(async () => resolveRead([savedTurn]));
    await waitFor(() => expect(versions).toEqual([10]));
  });

  test('recovers a definitively stale submission with a new key and current version', async () => {
    const savedTurn = { ...turns[0], turn_id: 'stale-then-current' };
    const requests: Array<{ key: string; version: number }> = [];
    const receipts = new Map<string, number>();
    sessionStorage.setItem(`epic_saved_brainstorm_send_${epicId}`, JSON.stringify({ conversationId, version: 2, expectedEpicVersion: 9 }));
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [] as T;
      if (path.endsWith(`/turns?project_id=${projectId}`)) return [savedTurn] as T;
      if (path.endsWith('/jobs') && init?.method === 'POST') {
        const key = new Headers(init.headers).get('Idempotency-Key')!;
        const version = JSON.parse(init.body as string).expected_epic_version;
        requests.push({ key, version });
        if (receipts.has(key)) return { job_id: 'accepted' } as T;
        if (version !== 10) throw new ApiError(409, 'conflict');
        receipts.set(key, version);
        return { job_id: 'accepted' } as T;
      }
      return undefined as T;
    });
    const view = render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry assistant help' }));
    await screen.findByText(/could not start the assistant/);
    view.rerender(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={10} />);
    await userEvent.click(screen.getByRole('button', { name: 'Retry assistant help' }));
    await waitFor(() => expect(receipts.size).toBe(1));
    expect(requests.map(request => request.version)).toEqual([9, 10]);
    expect(requests[1].key).not.toBe(requests[0].key);
  });

  test('replays an accepted uncertain submission after the epic changes without a second durable receipt', async () => {
    const savedTurn = { ...turns[0], turn_id: 'accepted-before-response-loss' };
    const requests: Array<{ key: string; body: string }> = [];
    const receipts = new Map<string, { body: string; job_id: string }>();
    let loseResponse = true;
    sessionStorage.setItem(`epic_saved_brainstorm_send_${epicId}`, JSON.stringify({ conversationId, version: 2, expectedEpicVersion: 9 }));
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [] as T;
      if (path.endsWith(`/turns?project_id=${projectId}`)) return [savedTurn] as T;
      if (path.endsWith('/jobs') && init?.method === 'POST') {
        const key = new Headers(init.headers).get('Idempotency-Key')!;
        const body = init.body as string;
        requests.push({ key, body });
        const replay = receipts.get(key);
        if (replay) {
          if (replay.body !== body) throw new Error('replay body changed');
          return { job_id: replay.job_id } as T;
        }
        if (JSON.parse(body).expected_epic_version !== 9) throw new ApiError(409, 'conflict');
        receipts.set(key, { body, job_id: 'accepted-once' });
        if (loseResponse) { loseResponse = false; throw new Error('response lost'); }
        return { job_id: 'accepted-once' } as T;
      }
      return undefined as T;
    });
    const first = render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry assistant help' }));
    await screen.findByRole('button', { name: 'Retry original request' });
    first.unmount();
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={10} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry original request' }));
    await waitFor(() => expect(requests).toHaveLength(2));
    expect(requests[1]).toEqual(requests[0]);
    expect(receipts.size).toBe(1);
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry assistant help' })).not.toBeInTheDocument());
  });

  test('new in-memory saved turn wins over stale persisted turn when writes fail', async () => {
    const savedKey = `epic_saved_brainstorm_send_${epicId}`;
    const oldConversation = 'old-conversation';
    const newConversation = 'new-conversation';
    sessionStorage.setItem(savedKey, JSON.stringify({ conversationId: oldConversation, version: 2, expectedEpicVersion: 9 }));
    const originalSet = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === savedKey) throw new DOMException('Storage denied');
      return originalSet.call(this, key, value);
    });
    let directReads = 0;
    const requests: string[] = [];
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations` && init?.method === 'POST') return { conversation_id: newConversation, version: 2 } as T;
      if (path.includes('/turns?project_id=') && !init?.signal) {
        if (++directReads === 1) throw new Error('read failed');
        return [{ ...turns[0], conversation_id: newConversation, turn_id: 'new-turn' }] as T;
      }
      if (path.endsWith('/jobs') && init?.method === 'POST') {
        requests.push(path);
        return { job_id: 'new-job' } as T;
      }
      return [] as T;
    });
    const first = render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.type(await screen.findByLabelText('What are you trying to accomplish?'), 'A new idea');
    await userEvent.click(screen.getByRole('button', { name: 'Start conversation' }));
    await screen.findByText(/could not start the assistant/);
    expect(JSON.parse(sessionStorage.getItem(savedKey)!).conversationId).toBe(oldConversation);
    first.unmount();
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={10} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry assistant help' }));
    await waitFor(() => expect(requests).toEqual([`/epics/${epicId}/brainstorm-conversations/${newConversation}/jobs`]));
  });

  test('a completed tombstone yields to a newer saved turn when removal and later writes fail', async () => {
    const savedKey = `epic_saved_brainstorm_send_${epicId}`;
    sessionStorage.setItem(savedKey, JSON.stringify({ conversationId: 'turn-A', version: 2, expectedEpicVersion: 9 }));
    const originalRemove = Storage.prototype.removeItem;
    const originalSet = Storage.prototype.setItem;
    let denyWrites = false;
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key) {
      if (key === savedKey) throw new DOMException('Removal denied');
      return originalRemove.call(this, key);
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === savedKey && denyWrites) throw new DOMException('Write denied');
      return originalSet.call(this, key, value);
    });
    const requests: string[] = [];
    let readB = 0;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return (requests.length
        ? [{ conversation_id: 'turn-A', conversation_version: 2, job_ids: ['job-1'] }]
        : []) as T;
      if (path.includes('/turn-A/turns') && init?.method === 'POST') return { version: 3 } as T;
      if (path.includes('/turn-A/turns?')) {
        if (!init?.signal && requests.length && ++readB === 1) throw new Error('read B failed');
        return requests.length
          ? [{ ...turns[0], conversation_id: 'turn-A', turn_id: 'prompt-A' }, { ...turns[0], conversation_id: 'turn-A', turn_id: 'prompt-B' }].slice(0, readB ? 2 : 1) as T
          : [{ ...turns[0], conversation_id: 'turn-A', turn_id: 'prompt-A' }] as T;
      }
      if (path.endsWith('/jobs') && init?.method === 'POST') {
        requests.push(path);
        return { job_id: `job-${requests.length}` } as T;
      }
      return [] as T;
    });
    const first = render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry assistant help' }));
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(JSON.parse(sessionStorage.getItem(savedKey)!).conversationId).toBe('turn-A');
    denyWrites = true;
    await screen.findByRole('button', { name: 'Send message' });
    await userEvent.type(screen.getByLabelText('What are you trying to accomplish?'), 'A new idea');
    await userEvent.click(screen.getByRole('button', { name: 'Send message' }));
    await screen.findByText(/could not start the assistant/);
    first.unmount();
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={10} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry assistant help' }));
    await waitFor(() => expect(requests).toEqual([
      `/epics/${epicId}/brainstorm-conversations/turn-A/jobs`,
      `/epics/${epicId}/brainstorm-conversations/turn-A/jobs`,
    ]));
    expect(readB).toBe(2);
  });

  test('replays an uncertain job submit exactly and does not offer it again after remount', async () => {
    const selectedRoute = { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' };
    const defaultRoute = { ...selectedRoute, model: 'gpt-6-luna', effort: 'medium' };
    const savedTurn = { ...turns[0], turn_id: 'uncertain-turn' };
    let startCount = 0;
    let jobCount = 0;
    const jobRequests: Array<[string, RequestInit | undefined]> = [];
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/projects/${projectId}/subscription-profile`) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
      if (path.startsWith('/subscription-runtime')) return { workers: [{ routes: [defaultRoute, selectedRoute] }] } as T;
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return (jobCount > 1
        ? [{ conversation_id: conversationId, conversation_version: 2, job_ids: ['uncertain-job'] }]
        : []) as T;
      if (path === `/epics/${epicId}/brainstorm-conversations` && init?.method === 'POST') {
        startCount += 1;
        return { conversation_id: conversationId, version: 2 } as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) return [savedTurn] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs` && init?.method === 'POST') {
        jobRequests.push([path, init]);
        jobCount += 1;
        if (jobCount === 1) throw new Error('job response lost');
        return { schema_version: 1, job_id: 'uncertain-job', job_version: 1, state: 'queued', replay_key: 'key' } as T;
      }
      if (path === `/epics/${epicId}/brainstorm-jobs/uncertain-job?project_id=${projectId}`) return { schema_version: 1, job_id: 'uncertain-job', job_version: 1, state: 'running', usage_known: null, process_settled: false, usage: null, unknown_usage_fields: [] } as T;
      return undefined as T;
    });
    const first = render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('default'));
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Model' }), JSON.stringify(['openai', 'codex_app_server', 'gpt-6-astra']));
    await userEvent.type(screen.getByLabelText('What are you trying to accomplish?'), 'One saved idea.');
    await userEvent.click(screen.getByRole('button', { name: 'Start conversation' }));
    await screen.findByRole('button', { name: 'Retry original request' });
    expect(startCount).toBe(1);
    expect(jobCount).toBe(1);

    await userEvent.click(screen.getByRole('button', { name: 'Retry original request' }));
    await waitFor(() => expect(jobCount).toBe(2));
    expect(jobRequests[1][0]).toBe(jobRequests[0][0]);
    expect(jobRequests[1][1]?.body).toBe(jobRequests[0][1]?.body);
    expect(JSON.parse(String(jobRequests[0][1]?.body)).requested_route).toEqual(selectedRoute);
    expect(new Headers(jobRequests[1][1]?.headers).get('Idempotency-Key')).toBe(new Headers(jobRequests[0][1]?.headers).get('Idempotency-Key'));
    await waitFor(() => expect(sessionStorage.getItem(`epic_saved_brainstorm_send_${epicId}`)).toBeNull());
    first.unmount();

    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await screen.findByText(/Job state: running/);
    expect(screen.queryByRole('button', { name: 'Retry assistant help' })).not.toBeInTheDocument();
    expect(startCount).toBe(1);
    expect(jobCount).toBe(2);
  });

  test.each([
    ['conversation-start', 'alternate'],
    ['conversation-start', 'default'],
    ['conversation-turn', 'alternate'],
    ['conversation-turn', 'default'],
  ] as const)('binds a recovered %s receipt to its own %s route despite older saved work', async (saveKind, routeKind) => {
    const oldRoute = { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' };
    const defaultRoute = { ...oldRoute, model: 'gpt-6-luna', effort: 'medium' };
    const newRoute = { ...oldRoute, model: 'gpt-6-sol', effort: 'high' };
    const newConversationId = saveKind === 'conversation-start' ? 'new-conversation' : conversationId;
    const newVersion = saveKind === 'conversation-start' ? 2 : 4;
    const newTurn = { ...turns[0], conversation_id: newConversationId, turn_id: 'new-operator-turn', text: 'A new idea.' };
    const receipt = saveKind === 'conversation-start'
      ? { conversation_id: newConversationId, version: newVersion }
      : { version: newVersion };
    const oldConversationId = saveKind === 'conversation-start' ? 'older-conversation' : conversationId;
    sessionStorage.setItem(`epic_saved_brainstorm_send_${epicId}`, JSON.stringify({
      conversationId: oldConversationId, version: 2, expectedEpicVersion: 9, route: oldRoute,
    }));
    let savedRequest: { body: string; key: string } | null = null;
    const jobs: RequestInit[] = [];
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/projects/${projectId}/subscription-profile`) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
      if (path.startsWith('/subscription-runtime')) return { workers: [{ routes: [defaultRoute, oldRoute, newRoute] }] } as T;
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return (saveKind === 'conversation-start'
        ? [] : [{ conversation_id: conversationId, conversation_version: 3, job_ids: [] }]) as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${newConversationId}/turns?project_id=${projectId}`)
        return (saveKind === 'conversation-start' ? [newTurn] : [...turns, newTurn]) as T;
      const savePath = saveKind === 'conversation-start'
        ? `/epics/${epicId}/brainstorm-conversations`
        : `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns`;
      if (path === savePath && init?.method === 'POST') {
        const body = String(init.body);
        const key = new Headers(init.headers).get('Idempotency-Key')!;
        if (savedRequest) {
          // Durable replay precedes the now-stale append version check.
          expect({ body, key }).toEqual(savedRequest);
          return receipt as T;
        }
        savedRequest = { body, key };
        throw new Error('save response lost after commit');
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/${newConversationId}/jobs` && init?.method === 'POST') {
        jobs.push(init);
        return { schema_version: 1, job_id: 'new-job', job_version: 1, state: 'queued', replay_key: 'job-key' } as T;
      }
      return undefined as T;
    });

    const first = render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    if (routeKind === 'alternate') {
      await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('default'));
      await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Model' }), JSON.stringify(['openai', 'codex_app_server', 'gpt-6-sol']));
    }
    await userEvent.type(await screen.findByLabelText('What are you trying to accomplish?'), newTurn.text);
    await userEvent.click(screen.getByRole('button', { name: saveKind === 'conversation-start' ? 'Start conversation' : 'Send message' }));
    await screen.findByRole('button', { name: 'Retry original request' });
    expect(jobs).toHaveLength(0);
    first.unmount();

    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={10} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry original request' }));
    await waitFor(() => expect(jobs).toHaveLength(1));
    const body = JSON.parse(String(jobs[0].body));
    expect(body.expected_epic_version).toBe(10);
    expect(body.prompt_turn_id).toBe(newTurn.turn_id);
    expect(body.expected_conversation_version).toBe(newVersion);
    if (routeKind === 'alternate') expect(body.requested_route).toEqual(newRoute);
    else expect(body).not.toHaveProperty('requested_route');
    expect(savedRequest).not.toBeNull();
  });

  test.each([
    ['conversation-start', undefined],
    ['conversation-turn', null],
  ] as const)('keeps a matching %s saved default (%s) despite an unrelated pending route', async (saveKind, savedRoute) => {
    const pendingRoute = { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' };
    const receipt = saveKind === 'conversation-start' ? { conversation_id: conversationId, version: 2 } : { version: 2 };
    const savePath = saveKind === 'conversation-start'
      ? `/epics/${epicId}/brainstorm-conversations`
      : `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns`;
    sessionStorage.setItem(`epic_saved_brainstorm_send_${epicId}`, JSON.stringify({
      conversationId, version: 2, expectedEpicVersion: 9, ...(savedRoute !== undefined ? { route: savedRoute } : {}),
    }));
    sessionStorage.setItem(`epic_pending_brainstorm_route_${epicId}`, JSON.stringify(pendingRoute));
    sessionStorage.setItem(`epic_pending_mutation_${epicId}`, JSON.stringify({
      kind: saveKind, method: 'POST', path: savePath,
      body: { schema_version: 1, project_id: projectId, text: 'Saved default idea.' },
      idempotencyKey: 'matching-save-key', timestamp: 123, uncertain: true,
    }));
    const jobs: RequestInit[] = [];
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [] as T;
      if (path === savePath && init?.method === 'POST') return receipt as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`)
        return [{ ...turns[0], text: 'Saved default idea.' }] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs` && init?.method === 'POST') {
        jobs.push(init);
        return { schema_version: 1, job_id: 'default-job', job_version: 1, state: 'queued', replay_key: 'job-key' } as T;
      }
      return undefined as T;
    });
    const first = render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    expect(jobs).toHaveLength(0);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry original request' }));
    await waitFor(() => expect(jobs).toHaveLength(1));
    expect(JSON.parse(String(jobs[0].body))).not.toHaveProperty('requested_route');
    expect(sessionStorage.getItem(`epic_saved_brainstorm_send_${epicId}`)).toBeNull();
    expect(sessionStorage.getItem(`epic_pending_brainstorm_route_${epicId}`)).toBeNull();
    first.unmount();
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    expect(jobs).toHaveLength(1);
  });

  test.each([
    ['wrong conversation', { ...turns[0], conversation_id: 'another-conversation' }],
    ['non-operator turn', { ...turns[0], role: 'assistant' as const }],
  ])('does not submit a saved %s as the prompt turn', async (_label, invalidTurn) => {
    sessionStorage.setItem(`epic_saved_brainstorm_send_${epicId}`, JSON.stringify({ conversationId, version: 2 }));
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) return [invalidTurn] as T;
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry assistant help' }));
    expect(await screen.findByText(/Your message is saved\./)).toBeInTheDocument();
    expect(vi.mocked(api).mock.calls.filter(([path, init]) => path.endsWith('/jobs') && init?.method === 'POST')).toHaveLength(0);
  });

  test.each(['setItem', 'removeItem', 'getItem'] as const)('storage.%s failure cannot offer a duplicate assistant request', async operation => {
    const savedKey = `epic_saved_brainstorm_send_${epicId}`;
    const savedTurn = { ...turns[0], turn_id: 'storage-safe-turn' };
    if (operation === 'getItem') sessionStorage.setItem(savedKey, JSON.stringify({ conversationId, version: 2 }));
    const storageMethods = {
      setItem: Storage.prototype.setItem,
      removeItem: Storage.prototype.removeItem,
      getItem: Storage.prototype.getItem,
    };
    const spy = operation === 'setItem'
      ? vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
          if (key === savedKey) throw new DOMException('Storage denied');
          return storageMethods.setItem.call(this, key, value);
        })
      : operation === 'removeItem'
        ? vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key) {
            if (key === savedKey) throw new DOMException('Storage denied');
            return storageMethods.removeItem.call(this, key);
          })
        : vi.spyOn(Storage.prototype, 'getItem').mockImplementation(function (this: Storage, key) {
            if (key === savedKey) throw new DOMException('Storage denied');
          return storageMethods.getItem.call(this, key);
          });
    let jobCount = 0;
    const jobRequests: RequestInit[] = [];
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return (jobCount
        ? [{ conversation_id: conversationId, conversation_version: 2, job_ids: ['storage-job'] }]
        : []) as T;
      if (path === `/epics/${epicId}/brainstorm-conversations` && init?.method === 'POST') return { conversation_id: conversationId, version: 2 } as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) return [savedTurn] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs` && init?.method === 'POST') {
        jobCount += 1;
        jobRequests.push(init);
        return { schema_version: 1, job_id: 'storage-job', job_version: 1, state: 'queued', replay_key: 'key' } as T;
      }
      if (path === `/epics/${epicId}/brainstorm-jobs/storage-job?project_id=${projectId}`) return { schema_version: 1, job_id: 'storage-job', job_version: 1, state: 'running', usage_known: null, process_settled: false, usage: null, unknown_usage_fields: [] } as T;
      return undefined as T;
    });
    try {
      const first = render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
      if (operation === 'getItem') {
        await screen.findByLabelText('What are you trying to accomplish?');
        expect(screen.queryByRole('button', { name: 'Retry assistant help' })).not.toBeInTheDocument();
        expect(jobCount).toBe(0);
      } else {
        await userEvent.type(screen.getByLabelText('What are you trying to accomplish?'), 'Start once');
        await userEvent.click(screen.getByRole('button', { name: 'Start conversation' }));
        await waitFor(() => expect(jobCount).toBe(1));
        expect(screen.queryByRole('button', { name: 'Retry assistant help' })).not.toBeInTheDocument();
        if (operation === 'removeItem') {
          first.unmount();
          render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
          await screen.findByText(/Job state: running/i);
          expect(screen.queryByRole('button', { name: 'Retry assistant help' })).not.toBeInTheDocument();
          expect(jobCount).toBe(1);
        }
      }
    } finally {
      spy.mockRestore();
    }
  });

  test.each([
    '{broken json',
    JSON.stringify({ conversationId: '../other', version: 2 }),
    JSON.stringify({ conversationId, version: 1 }),
    JSON.stringify({ conversationId, version: 2, expectedEpicVersion: 'not-a-version' }),
  ])('ignores malformed saved-send recovery data: %s', async stored => {
    sessionStorage.setItem(`epic_saved_brainstorm_send_${epicId}`, stored);
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [] as T;
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await screen.findByLabelText('What are you trying to accomplish?');
    expect(screen.queryByRole('button', { name: 'Retry assistant help' })).not.toBeInTheDocument();
    expect(vi.mocked(api).mock.calls.filter(([path, init]) => path.endsWith('/jobs') && init?.method === 'POST')).toHaveLength(0);
  });

  test('holds one send lock while the saved conversation read is pending', async () => {
    const selectedRoute = { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' };
    const defaultRoute = { ...selectedRoute, model: 'gpt-6-luna', effort: 'medium' };
    const firstThread = { ...thread, conversation_version: 2, job_ids: [] };
    const secondThread = { conversation_id: 'other-conversation', conversation_version: 2, job_ids: [] };
    const followUp = { ...turns[0], turn_id: 'locked-follow-up', text: 'Second depot context.' };
    let startCount = 0;
    let appendCount = 0;
    let jobCount = 0;
    let releaseTurns!: (value: typeof turns) => void;
    const turnRead = new Promise<typeof turns>(resolve => { releaseTurns = resolve; });
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/projects/${projectId}/subscription-profile`) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
      if (path.startsWith('/subscription-runtime')) return { workers: [{ routes: [defaultRoute, selectedRoute] }] } as T;
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [firstThread, secondThread] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) {
        if (!init?.signal) return await turnRead as T;
        return [turns[0]] as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns` && init?.method === 'POST') {
        appendCount += 1;
        return { version: 3 } as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs` && init?.method === 'POST') {
        jobCount += 1;
        return { schema_version: 1, job_id: 'locked-job', job_version: 1, state: 'queued', replay_key: 'key' } as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations` && init?.method === 'POST') { startCount += 1; return { conversation_id: conversationId, version: 2 } as T; }
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('default'));
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Model' }), JSON.stringify(['openai', 'codex_app_server', 'gpt-6-astra']));
    const composer = await screen.findByLabelText('What are you trying to accomplish?');
    await userEvent.type(composer, followUp.text);
    await userEvent.click(screen.getByRole('button', { name: 'Send message' }));
    await screen.findByRole('button', { name: 'Starting assistant…' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Model' }), 'default');
    expect(composer).toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'Conversation' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Sending…' }));
    expect(startCount).toBe(0);
    expect(appendCount).toBe(1);
    expect(jobCount).toBe(0);

    releaseTurns([turns[0], followUp]);
    await waitFor(() => expect(jobCount).toBe(1));
    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs`, expect.objectContaining({
      method: 'POST', body: JSON.stringify({ schema_version: 1, project_id: projectId, prompt_turn_id: followUp.turn_id, expected_epic_version: 9, expected_conversation_version: 3, requested_route: selectedRoute }),
    }));
  });

  test.each(['old-first', 'new-first'] as const)('an unmounted saved-turn read cannot submit after remount recovery (%s)', async order => {
    const selectedRoute = { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' };
    const defaultRoute = { ...selectedRoute, model: 'gpt-6-luna', effort: 'medium' };
    const savedTurn = { ...turns[0], turn_id: 'deferred-recovery-turn', text: 'Track depot repairs better.' };
    const persistedTurns = [turns[0], turns[1], savedTurn];
    const pendingReads: Array<(value: typeof persistedTurns) => void> = [];
    const sentJobs: Array<{ path: string; init?: RequestInit }> = [];
    const originalSetItem = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === `epic_saved_brainstorm_send_${epicId}`) throw new DOMException('Storage denied');
      return originalSetItem.call(this, key, value);
    });
    const savedThread = { ...thread, conversation_version: 3, job_ids: ['older-job'] };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/projects/${projectId}/subscription-profile`) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
      if (path.startsWith('/subscription-runtime')) return { workers: [{ routes: [defaultRoute, selectedRoute] }] } as T;
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [savedThread] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) {
        if (!init?.signal) return await new Promise<typeof persistedTurns>(resolve => pendingReads.push(resolve)) as T;
        return persistedTurns.slice(0, 2) as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns` && init?.method === 'POST') return { version: 4 } as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs` && init?.method === 'POST') {
        sentJobs.push({ path, init });
        return { schema_version: 1, job_id: 'recovered-once', job_version: 1, state: 'queued', replay_key: 'key' } as T;
      }
      if (path === `/epics/${epicId}/brainstorm-jobs/older-job?project_id=${projectId}`) return { ...proposedOutcome, job_id: 'older-job' } as T;
      return undefined as T;
    });

    const firstMount = render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('default'));
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Model' }), JSON.stringify(['openai', 'codex_app_server', 'gpt-6-astra']));
    await userEvent.type(await screen.findByLabelText('What are you trying to accomplish?'), savedTurn.text);
    await userEvent.click(screen.getByRole('button', { name: 'Send message' }));
    await waitFor(() => expect(pendingReads).toHaveLength(1));
    firstMount.unmount();

    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={10} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry assistant help' }));
    await waitFor(() => expect(pendingReads).toHaveLength(2));
    await act(async () => pendingReads[order === 'old-first' ? 0 : 1](persistedTurns));
    expect(sentJobs).toHaveLength(order === 'old-first' ? 0 : 1);
    await act(async () => pendingReads[order === 'old-first' ? 1 : 0](persistedTurns));
    await waitFor(() => expect(sentJobs).toHaveLength(1));
    expect(sentJobs[0].init?.body).toBe(JSON.stringify({
      schema_version: 1, project_id: projectId, prompt_turn_id: savedTurn.turn_id, expected_epic_version: 10, expected_conversation_version: 4,
      requested_route: selectedRoute,
    }));
  });

  test('a save receipt arriving after unmount waits for explicit exact save replay before assistance', async () => {
    const selectedRoute = { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'low', auth_mode: 'subscription', billing_mode: 'allowance_only' };
    const defaultRoute = { ...selectedRoute, model: 'gpt-6-luna', effort: 'medium' };
    const savedTurn = { ...turns[0], turn_id: 'deferred-save-turn', conversation_id: 'deferred-save-conversation' };
    let resolveFirstSave!: (value: { conversation_id: string; version: number }) => void;
    const saveRequests: RequestInit[] = [];
    const jobRequests: RequestInit[] = [];
    let directReads = 0;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/projects/${projectId}/subscription-profile`) return { preferences: [{ purpose: 'exploration', preferred_route: defaultRoute }] } as T;
      if (path.startsWith('/subscription-runtime')) return { workers: [{ routes: [defaultRoute, selectedRoute] }] } as T;
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations` && init?.method === 'POST') {
        saveRequests.push(init);
        if (saveRequests.length === 1) return await new Promise<{ conversation_id: string; version: number }>(resolve => { resolveFirstSave = resolve; }) as T;
        return { conversation_id: 'deferred-save-conversation', version: 2 } as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/${savedTurn.conversation_id}/turns?project_id=${projectId}`) {
        if (!init?.signal) directReads += 1;
        return [savedTurn] as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/${savedTurn.conversation_id}/jobs` && init?.method === 'POST') {
        jobRequests.push(init);
        return { schema_version: 1, job_id: 'deferred-save-job', job_version: 1, state: 'queued', replay_key: 'key' } as T;
      }
      return undefined as T;
    });

    const firstMount = render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('default'));
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Model' }), JSON.stringify(['openai', 'codex_app_server', 'gpt-6-astra']));
    await userEvent.type(await screen.findByLabelText('What are you trying to accomplish?'), 'Start a repair tracker.');
    await userEvent.click(screen.getByRole('button', { name: 'Start conversation' }));
    await waitFor(() => expect(saveRequests).toHaveLength(1));
    firstMount.unmount();
    await act(async () => resolveFirstSave({ conversation_id: 'deferred-save-conversation', version: 2 }));
    await waitFor(() => expect(JSON.parse(sessionStorage.getItem(`epic_saved_brainstorm_send_${epicId}`) ?? 'null')).toMatchObject({
      conversationId: 'deferred-save-conversation', version: 2, expectedEpicVersion: 9, route: selectedRoute,
    }));
    expect(directReads).toBe(0);
    expect(jobRequests).toHaveLength(0);

    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={10} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry original request' }));
    await waitFor(() => expect(jobRequests).toHaveLength(1));
    expect(saveRequests[1].body).toBe(saveRequests[0].body);
    expect(new Headers(saveRequests[1].headers).get('Idempotency-Key')).toBe(new Headers(saveRequests[0].headers).get('Idempotency-Key'));
    expect(directReads).toBe(1);
    expect(jobRequests[0].body).toBe(JSON.stringify({
      schema_version: 1, project_id: projectId, prompt_turn_id: savedTurn.turn_id, expected_epic_version: 10, expected_conversation_version: 2,
      requested_route: selectedRoute,
    }));
  });

  test('a newly observed unrelated job does not hide saved-turn recovery', async () => {
    const savedTurn = { ...turns[0], turn_id: 'unrelated-job-turn' };
    let threadReads = 0;
    let directReads = 0;
    const savedThread = { ...thread, conversation_version: 3, job_ids: ['older-job'] };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) {
        threadReads += 1;
        return [{ ...savedThread, job_ids: threadReads > 1 ? ['older-job', 'unrelated-new-job'] : ['older-job'] }] as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) {
        if (!init?.signal) { directReads += 1; if (directReads === 1) throw new Error('read not ready'); }
        return [turns[0], turns[1], savedTurn] as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns` && init?.method === 'POST') return { version: 4 } as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs` && init?.method === 'POST') return { schema_version: 1, job_id: 'new-job', job_version: 1, state: 'queued', replay_key: 'key' } as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/older-job?project_id=${projectId}` || path === `/epics/${epicId}/brainstorm-jobs/unrelated-new-job?project_id=${projectId}`) return { ...proposedOutcome, job_id: path.includes('older-job') ? 'older-job' : 'unrelated-new-job' } as T;
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.type(await screen.findByLabelText('What are you trying to accomplish?'), 'A new follow-up');
    await userEvent.click(screen.getByRole('button', { name: 'Send message' }));
    await screen.findByRole('alert');
    await waitFor(() => expect(threadReads).toBeGreaterThan(1));
    expect(await screen.findByRole('button', { name: 'Retry assistant help' })).toBeInTheDocument();
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

  test('explains how to recover when the selected AI model cannot start', async () => {
    const failed = { ...proposedOutcome, state: 'failed', proposal: null, proposal_digest: null, failure: 'unavailable', process_settled: true };
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) return turns as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/current-job?project_id=${projectId}`) return failed as T;
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    expect(await screen.findByText(/The selected AI model could not start/)).toBeInTheDocument();
    expect(screen.getByText('The job reported: unavailable.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry assistant job' })).toBeInTheDocument();
  });

  test('submits owner override and note when retrying a failed assistant job', async () => {
    const failed = { ...proposedOutcome, state: 'failed', proposal: null, proposal_digest: null, failure: 'quota_exhausted', process_settled: true };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns?project_id=${projectId}`) return turns as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/current-job?project_id=${projectId}`) return failed as T;
      if (path === `/epics/${epicId}/brainstorm-jobs/current-job/retry` && init?.method === 'POST') return { schema_version: 1, job_id: 'current-job', job_version: 8, state: 'queued', replay_key: 'key' } as T;
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await screen.findByRole('button', { name: 'Retry assistant job' });

    await userEvent.click(screen.getByLabelText('Owner override retry policy'));
    await userEvent.type(screen.getByPlaceholderText('Optional override note...'), 'Emergency operator override');
    await userEvent.click(screen.getByRole('button', { name: 'Retry assistant job' }));

    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-jobs/current-job/retry`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        schema_version: 1,
        project_id: projectId,
        expected_job_version: 7,
        owner_override: true,
        override_note: 'Emergency operator override',
      }),
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
    sessionStorage.setItem(`epic_saved_brainstorm_send_${epicId}`, JSON.stringify({ conversationId: 'thread-A', version: 5 }));
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
    expect(sessionStorage.getItem(`epic_saved_brainstorm_send_${epicId}`)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Retry assistant help' })).not.toBeInTheDocument();
  });

  test('clears a whitespace-padded prompt through the same completion path after replay', async () => {
    let startCalls = 0;
    let jobCalls = 0;
    const savedTurn = { ...turns[0], turn_id: 'created-turn', conversation_id: 'created-conversation' };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/brainstorm-conversations?`)) return [] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations` && init?.method === 'POST') {
        startCalls += 1;
        if (startCalls === 1) throw new Error('connection lost');
        return { conversation_id: 'created-conversation', version: 2 } as T;
      }
      if (path === `/epics/${epicId}/brainstorm-conversations/created-conversation/turns?project_id=${projectId}`) return [savedTurn] as T;
      if (path === `/epics/${epicId}/brainstorm-conversations/created-conversation/jobs` && init?.method === 'POST') {
        jobCalls += 1;
        return { schema_version: 1, job_id: 'created-job', job_version: 1, state: 'queued', replay_key: 'job-key' } as T;
      }
      return undefined as T;
    });
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    const input = screen.getByLabelText('What are you trying to accomplish?');
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
    await waitFor(() => expect(jobCalls).toBe(1));
    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/brainstorm-conversations/created-conversation/jobs`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ schema_version: 1, project_id: projectId, prompt_turn_id: 'created-turn', expected_epic_version: 9, expected_conversation_version: 2 }),
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

  test('keeps brainstorming focused and removes obsolete unavailable contract placeholder', async () => {
    mockAuthoring();
    render(<AuthoringWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);

    expect(await screen.findByText('Authentication is difficult to audit.')).toBeInTheDocument();
    expect(screen.queryByText('There is no available graph decomposition request contract yet.')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Decomposition message or instruction')).not.toBeInTheDocument();
  });
});
