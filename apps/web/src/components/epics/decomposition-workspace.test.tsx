import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderToString } from 'react-dom/server';
import { hydrateRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { DecompositionWorkspace } from './decomposition-workspace';
import { api } from '@/lib/api/client';
import { EMPTY_BRIEF, type DecompositionProposal, type AuthoringOutcome, type BrainstormTurn, type EpicResponse, type GraphRevisionResponse, type AcceptedGraphResponse } from '@/hooks/epics/types';
import { EpicWorkspaceProvider, useEpicWorkspace } from '@/hooks/epics/use-epic-workspace';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) {
      super(code);
    }
  },
}));

describe('DecompositionWorkspace', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';
  const projectId = '22222222-2222-4222-8222-222222222222';
  const conversationId = '33333333-3333-4333-8333-333333333333';
  const turnId = '44444444-4444-4444-8444-444444444444';
  const proposalTurnId = '55555555-5555-4555-8555-555555555555';
  const briefRevisionId = '66666666-6666-4666-8666-666666666666';

  const proposal: DecompositionProposal = {
    schema_version: 1,
    turn_id: proposalTurnId,
    epic_id: epicId,
    project_id: projectId,
    brief_revision_id: briefRevisionId,
    brief_digest: 'd'.repeat(64),
    problem: 'Authentication audit trail lacks structured work items.',
    summary: 'Decompose auth implementation into audit log schema and event emission.',
    items: [
      {
        item_id: 'item-1111-1111-1111-111111111111',
        title: 'Define audit log database migration',
        outcome: 'PostgreSQL migration creates audit_events table with indices.',
        disposition: 'required',
        ordinal: 1,
        acceptance_criteria: ['Migration applies cleanly on empty database', 'Index on created_at exists'],
        dependency_item_ids: [],
        source_requirement_ids: ['req-auth-trace'],
      },
      {
        item_id: 'item-2222-2222-2222-222222222222',
        title: 'Emit login audit event in session handler',
        outcome: 'Session service writes audit record on successful and failed token generation.',
        disposition: 'required',
        ordinal: 2,
        acceptance_criteria: ['Login records operator user_id', 'Failure records rejection reason'],
        dependency_item_ids: ['item-1111-1111-1111-111111111111'],
        source_requirement_ids: ['req-auth-trace'],
      },
    ],
    assumptions: ['Database migrations run before services start'],
    open_questions: ['Retention policy duration in production'],
    resolved_turn_ids: [],
    evidence: [
      {
        schema_version: 1,
        path: 'docs/architecture/audit.md',
        content_digest: 'e'.repeat(64),
        excerpt: 'All authentication events must write to audit_events.',
      },
    ],
  };

  const thread = {
    conversation_id: conversationId,
    conversation_version: 2,
    job_ids: ['job-old', 'decomp-job-1'],
  };

  const turns = [
    {
      schema_version: 1 as const,
      turn_id: turnId,
      conversation_id: conversationId,
      role: 'operator' as const,
      text: 'Break down the audit requirements into database and service work items.',
      pending: false,
      proposal: null,
    },
    {
      schema_version: 1 as const,
      turn_id: proposalTurnId,
      conversation_id: conversationId,
      role: 'assistant' as const,
      text: 'Here is the proposed decomposition.',
      pending: false,
      proposal,
    },
  ];

  const proposedOutcome: AuthoringOutcome = {
    schema_version: 1,
    job_id: 'decomp-job-1',
    job_version: 3,
    state: 'proposed',
    proposal_digest: 'f'.repeat(64),
    proposal,
    adopted_revision_id: null,
    failure: null,
    usage_known: true,
    process_settled: true,
    usage: {
      schema_version: 1,
      duration_ms: 450,
      duration_lower_bound_ms: 400,
      tool_call_count: 3,
      input_tokens: 1200,
      output_tokens: 350,
      estimated_api_cost_minor: 12,
      unknown_fields: [],
    },
    reservation: null,
    cumulative_usage: {
      schema_version: 1,
      duration_ms: 450,
      tool_call_count: 3,
      input_tokens: 1200,
      output_tokens: 350,
      estimated_api_cost_minor: 12,
    },
    held_reservations: {
      schema_version: 1,
      duration_ms: 0,
      tool_call_count: 0,
      input_tokens: 0,
      output_tokens: 0,
      estimated_api_cost_minor: 0,
    },
    uncertain_attempts: 0,
    currency: 'USD',
    unknown_usage_fields: [],
    held_reasons: {
      schema_version: 1,
      duration_ms: null,
      tool_call_count: null,
      input_tokens: null,
      output_tokens: null,
      estimated_api_cost_minor: null,
    },
  };

  function mockDecomposition(outcome: AuthoringOutcome = proposedOutcome, threadList = [thread], turnList = turns) {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/decomposition-conversations?`)) return threadList as T;
      if (path === `/epics/${epicId}/decomposition-conversations/${conversationId}/turns?project_id=${projectId}`) return turnList as T;
      if (path === `/epics/${epicId}/decomposition-jobs/decomp-job-1?project_id=${projectId}`) return outcome as T;
      if (path === `/epics/${epicId}/decomposition-jobs/decomp-job-1/adopt` && init?.method === 'POST') {
        return {
          schema_version: 1,
          graph_revision_id: 'graph-rev-999',
          graph_digest: 'g'.repeat(64),
          job_version: 3,
          epic_version: 10,
        } as T;
      }
      if (path === `/epics/${epicId}/decomposition-conversations/${conversationId}/jobs` && init?.method === 'POST') {
        return {
          schema_version: 1,
          job_id: 'new-decomp-job',
          job_version: 1,
          state: 'queued',
          replay_key: 'rk-decomp',
        } as T;
      }
      if (path.startsWith(`/epics/${epicId}/brief-revisions`)) return [] as T;
      if (path.startsWith(`/epics/${epicId}/graph-revisions`)) return [] as T;
      if (path === `/epics/${epicId}`) return { epic_id: epicId, version: 9, project_id: projectId } as T;
      return undefined as T;
    });
  }

  beforeEach(() => {
    resetEpicMutationStoreForTesting();
    vi.clearAllMocks();
    sessionStorage.clear();
    window.history.replaceState({}, '', `/epics/${epicId}`);
  });

  afterEach(() => {
    resetEpicMutationStoreForTesting();
    cleanup();
    vi.mocked(api).mockReset();
    sessionStorage.clear();
  });

  test('URL conversation selection restores without hydration mismatch', async () => {
    window.history.replaceState({}, '', `/epics/${epicId}?decomp_conversation_id=${conversationId}`);
    vi.mocked(api).mockImplementation(() => new Promise(() => {}));
    const browserWindow = window;
    vi.stubGlobal('window', undefined);
    let serverMarkup: string;
    try {
      serverMarkup = renderToString(<DecompositionWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    } finally {
      vi.stubGlobal('window', browserWindow);
    }
    const container = document.createElement('div');
    document.body.append(container);
    container.innerHTML = serverMarkup;
    const recoverableError = vi.fn();
    const root = hydrateRoot(
      container,
      <DecompositionWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />,
      { onRecoverableError: recoverableError }
    );
    try {
      await waitFor(() =>
        expect(container.querySelector('button[type="submit"]')).toHaveTextContent('Send decomposition message')
      );
      expect(recoverableError).not.toHaveBeenCalled();
    } finally {
      await act(async () => { root.unmount(); });
      container.remove();
      vi.unstubAllGlobals();
    }
  });

  test('renders decomposition proposal preview with items, edges, and adopts exact job digest and versions', async () => {
    mockDecomposition();
    render(<DecompositionWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);

    expect(await screen.findByText('Authentication audit trail lacks structured work items.')).toBeInTheDocument();
    expect(screen.getByText('Decompose auth implementation into audit log schema and event emission.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Proposed decomposition', level: 3 })).toBeInTheDocument();
    expect(screen.getByText(/Proposed work items \(2\)/)).toBeInTheDocument();
    expect(screen.getByText('1. Define audit log database migration')).toBeInTheDocument();
    expect(screen.getByText('2. Emit login audit event in session handler')).toBeInTheDocument();
    expect(screen.getByText('Migration applies cleanly on empty database')).toBeInTheDocument();
    expect(screen.getByText('item-1111-1111-1111-111111111111')).toBeInTheDocument();
    expect(screen.getAllByText('req-auth-trace')).toHaveLength(2);
    expect(screen.getByText('Database migrations run before services start')).toBeInTheDocument();
    expect(screen.getByText('Retention policy duration in production')).toBeInTheDocument();
    expect(screen.getByText(/All authentication events must write to audit_events/)).toBeInTheDocument();

    const adoptBtn = screen.getByRole('button', { name: 'Adopt proposed decomposition' });
    await userEvent.click(adoptBtn);

    expect(api).toHaveBeenCalledWith(
      `/epics/${epicId}/decomposition-jobs/decomp-job-1/adopt`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          schema_version: 1,
          project_id: projectId,
          expected_job_version: 3,
          expected_epic_version: 9,
          proposal_digest: 'f'.repeat(64),
        }),
      })
    );
  });

  test('submits latest operator turn to generate proposed decomposition', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/decomposition-conversations?`)) {
        return [{ ...thread, job_ids: [] }] as T;
      }
      if (path === `/epics/${epicId}/decomposition-conversations/${conversationId}/turns?project_id=${projectId}`) {
        return turns as T;
      }
      if (path === `/epics/${epicId}/decomposition-conversations/${conversationId}/jobs` && init?.method === 'POST') {
        return {
          schema_version: 1,
          job_id: 'new-decomp-job',
          job_version: 1,
          state: 'queued',
          replay_key: 'rk-decomp',
        } as T;
      }
      return undefined as T;
    });

    render(<DecompositionWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await screen.findByText('Here is the proposed decomposition.');

    const draftBtn = screen.getByRole('button', { name: 'Generate proposed decomposition' });
    await userEvent.click(draftBtn);

    await waitFor(() =>
      expect(api).toHaveBeenCalledWith(
        `/epics/${epicId}/decomposition-conversations/${conversationId}/jobs`,
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({
            schema_version: 1,
            project_id: projectId,
            prompt_turn_id: turns[0].turn_id,
            expected_epic_version: 9,
            expected_conversation_version: 2,
          }),
        })
      )
    );
  });

  test('stale response / rejection preserves proposal preview and prompt input', async () => {
    mockDecomposition();
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/decomposition-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/decomposition-conversations/${conversationId}/turns?project_id=${projectId}`) return turns as T;
      if (path === `/epics/${epicId}/decomposition-jobs/decomp-job-1?project_id=${projectId}`) return proposedOutcome as T;
      if (path === `/epics/${epicId}/decomposition-jobs/decomp-job-1/adopt` && init?.method === 'POST') {
        const ApiError = (await import('@/lib/api/client')).ApiError;
        throw new ApiError(409, 'stale_version', {});
      }
      return undefined as T;
    });

    render(<DecompositionWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await screen.findByText('Authentication audit trail lacks structured work items.');
    await userEvent.type(screen.getByLabelText('Decomposition message or instruction'), 'Keep this unsent instruction');

    const adoptBtn = screen.getByRole('button', { name: 'Adopt proposed decomposition' });
    await userEvent.click(adoptBtn);

    // Stale error is shown
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    // Proposal preview is retained
    expect(screen.getByText('Authentication audit trail lacks structured work items.')).toBeInTheDocument();
    expect(screen.getByText('1. Define audit log database migration')).toBeInTheDocument();
    expect(screen.getByLabelText('Decomposition message or instruction')).toHaveValue('Keep this unsent instruction');
  });

  test('retains multiple conversations and jobs for the same epic', async () => {
    const thread2 = {
      conversation_id: 'conv-2222',
      conversation_version: 1,
      job_ids: ['job-222'],
    };
    mockDecomposition(proposedOutcome, [thread, thread2]);

    render(<DecompositionWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await screen.findByText('Conversation 1');

    const convSelect = screen.getByLabelText('Decomposition conversation');
    expect(convSelect).toHaveValue(conversationId);

    await userEvent.selectOptions(convSelect, 'conv-2222');
    expect(convSelect).toHaveValue('conv-2222');
  });

  test('first refusal with no admitted process reports settlement and usage not reported', async () => {
    const firstRefusalOutcome: AuthoringOutcome = {
      schema_version: 1,
      job_id: 'decomp-job-1',
      job_version: 1,
      state: 'failed',
      failure: 'model_refusal',
      proposal: null,
      proposal_digest: null,
      usage_known: null,
      process_settled: true,
      usage: null,
      unknown_usage_fields: [],
    };
    mockDecomposition(firstRefusalOutcome);

    render(<DecompositionWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    expect(await screen.findByText('The job reported: model refusal.')).toBeInTheDocument();
    expect(screen.getByText('Usage: not yet reported.')).toBeInTheDocument();
    expect(screen.getByText('Process settled')).toBeInTheDocument();
  });

  test('failed settled job allows owner retry with omitted note and explicit note', async () => {
    const failedOutcome: AuthoringOutcome = {
      schema_version: 1,
      job_id: 'decomp-job-1',
      job_version: 2,
      state: 'failed',
      failure: 'rate_limit_exceeded',
      proposal: null,
      proposal_digest: null,
      usage_known: false,
      process_settled: true,
      usage: null,
      unknown_usage_fields: [],
    };
    mockDecomposition(failedOutcome);

    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/decomposition-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/decomposition-conversations/${conversationId}/turns?project_id=${projectId}`) return turns as T;
      if (path === `/epics/${epicId}/decomposition-jobs/decomp-job-1?project_id=${projectId}`) return failedOutcome as T;
      if (path === `/epics/${epicId}/decomposition-jobs/decomp-job-1/retry` && init?.method === 'POST') {
        return {
          schema_version: 1,
          job_id: 'decomp-job-1',
          job_version: 3,
          state: 'queued',
          replay_key: 'rk-retry',
        } as T;
      }
      return undefined as T;
    });

    render(<DecompositionWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    expect(await screen.findByRole('button', { name: 'Retry decomposition job' })).toBeInTheDocument();

    // 1. Retry without owner override sends owner_override: false
    await userEvent.click(screen.getByRole('button', { name: 'Retry decomposition job' }));
    expect(api).toHaveBeenCalledWith(
      `/epics/${epicId}/decomposition-jobs/decomp-job-1/retry`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          project_id: projectId,
          expected_job_version: 2,
          owner_override: false,
        }),
      })
    );

    // 2. Retry with owner override and omitted note
    const overrideCheckbox = screen.getByLabelText('Owner override decomposition retry policy');
    await userEvent.click(overrideCheckbox);
    expect(overrideCheckbox).toBeChecked();

    await userEvent.click(screen.getByRole('button', { name: 'Retry decomposition job' }));
    expect(api).toHaveBeenCalledWith(
      `/epics/${epicId}/decomposition-jobs/decomp-job-1/retry`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          project_id: projectId,
          expected_job_version: 2,
          owner_override: true,
        }),
      })
    );

    // 3. Retry with owner override and explicit note
    const noteInput = screen.getByLabelText('Decomposition override note');
    await userEvent.type(noteInput, 'Operator authorized retry');

    await userEvent.click(screen.getByRole('button', { name: 'Retry decomposition job' }));
    expect(api).toHaveBeenCalledWith(
      `/epics/${epicId}/decomposition-jobs/decomp-job-1/retry`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          project_id: projectId,
          expected_job_version: 2,
          owner_override: true,
          override_note: 'Operator authorized retry',
        }),
      })
    );
  });

  test('live job cancellation requests cancel and distinguishes pending settlement', async () => {
    let currentOutcome: AuthoringOutcome = {
      schema_version: 1,
      job_id: 'decomp-job-1',
      job_version: 2,
      state: 'running',
      failure: null,
      proposal: null,
      proposal_digest: null,
      usage_known: null,
      process_settled: false,
      usage: null,
      unknown_usage_fields: [],
    };

    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/decomposition-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/decomposition-conversations/${conversationId}/turns?project_id=${projectId}`) return turns as T;
      if (path === `/epics/${epicId}/decomposition-jobs/decomp-job-1?project_id=${projectId}`) return currentOutcome as T;
      if (path === `/epics/${epicId}/decomposition-jobs/decomp-job-1/cancel` && init?.method === 'POST') {
        currentOutcome = {
          ...currentOutcome,
          state: 'cancel_requested',
          job_version: 3,
        };
        return {
          schema_version: 1,
          job_id: 'decomp-job-1',
          job_version: 3,
          state: 'cancel_requested',
          replay_key: 'rk-cancel',
        } as T;
      }
      return undefined as T;
    });

    render(<DecompositionWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    expect(await screen.findByText('Process settlement pending')).toBeInTheDocument();

    const cancelBtn = screen.getByRole('button', { name: 'Cancel decomposition job' });
    await userEvent.click(cancelBtn);

    expect(api).toHaveBeenCalledWith(
      `/epics/${epicId}/decomposition-jobs/decomp-job-1/cancel`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          schema_version: 1,
          project_id: projectId,
          expected_job_version: 2,
        }),
      })
    );
  });

  test('starts a saved conversation and appends a prompt against its latest version', async () => {
    let savedThread: typeof thread | null = null;
    let savedTurns: BrainstormTurn[] = [];
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/decomposition-conversations` && init?.method === 'POST') {
        const body = JSON.parse(init.body as string);
        savedThread = { ...thread, conversation_version: 1, job_ids: [] };
        savedTurns = [{ ...turns[0], text: body.text }];
        return { conversation_id: conversationId, version: 1 } as T;
      }
      if (path === `/epics/${epicId}/decomposition-conversations/${conversationId}/turns` && init?.method === 'POST') {
        const body = JSON.parse(init.body as string);
        expect(body.expected_conversation_version).toBe(1);
        savedThread = { ...thread, conversation_version: 2, job_ids: [] };
        savedTurns = [...savedTurns, { ...turns[0], turn_id: proposalTurnId, text: body.text }];
        return { version: 2 } as T;
      }
      if (path.startsWith(`/epics/${epicId}/decomposition-conversations?`)) return (savedThread ? [savedThread] : []) as T;
      if (path.startsWith(`/epics/${epicId}/decomposition-conversations/${conversationId}/turns?`)) return savedTurns as T;
      return undefined as T;
    });
    render(<DecompositionWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.type(screen.getByLabelText('Decomposition message or instruction'), 'Start with the audit schema');
    await userEvent.click(screen.getByRole('button', { name: 'Start decomposition conversation' }));
    expect(await screen.findByText('Start with the audit schema')).toBeInTheDocument();
    expect(screen.getByLabelText('Decomposition message or instruction')).toHaveValue('');
    await userEvent.type(screen.getByLabelText('Decomposition message or instruction'), 'Then add session events');
    await userEvent.click(screen.getByRole('button', { name: 'Send decomposition message' }));
    expect(await screen.findByText('Then add session events')).toBeInTheDocument();
    expect(screen.getByLabelText('Decomposition message or instruction')).toHaveValue('');
    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/decomposition-conversations/${conversationId}/turns`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ schema_version: 1, project_id: projectId, expected_conversation_version: 1, text: 'Then add session events', pending: false }),
    }));
  });

  test('lost adoption response survives remount, replays exact identity and refreshes the accepted saved graph', async () => {
    const graphId = '77777777-7777-4777-8777-777777777777';
    const graphDigest = 'a'.repeat(64);
    let savedEpic: EpicResponse = {
      schema_version: 1, epic_id: epicId, project_id: projectId, title: 'Audit trail', version: 9,
      draft: EMPTY_BRIEF, created_at: '2026-10-06T00:00:00Z', updated_at: '2026-10-06T00:00:00Z',
      accepted_brief_revision_id: null, accepted_brief_digest: null, accepted_graph_revision_id: null, accepted_graph_digest: null,
    };
    const graph: GraphRevisionResponse = {
      schema_version: 1, epic_id: epicId, epic_version: 10, graph_revision_id: graphId, graph_digest: graphDigest,
      brief_revision_id: briefRevisionId, brief_digest: proposal.brief_digest, revision_number: 1,
      created_at: '2026-10-06T00:00:01Z',
      items: proposal.items.map(item => ({ ...item, graph_revision_id: graphId, item_digest: 'b'.repeat(64) })),
    };
    const acceptedGraph: AcceptedGraphResponse = {
      schema_version: 1, epic_id: epicId, graph_revision_id: graphId, graph_digest: graphDigest,
      brief_revision_id: briefRevisionId, brief_digest: proposal.brief_digest, items: graph.items,
    };
    const receipt = { schema_version: 1, graph_revision_id: graphId, graph_digest: graphDigest, job_version: 4, epic_version: 10 };
    const adoptionRequests: RequestInit[] = [];
    let adoptionCount = 0;
    let currentOutcome = proposedOutcome;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/decomposition-jobs/decomp-job-1/adopt` && init?.method === 'POST') {
        adoptionRequests.push(init);
        if (adoptionRequests.length === 1) {
          adoptionCount++;
          savedEpic = { ...savedEpic, version: 10, accepted_graph_revision_id: graphId, accepted_graph_digest: graphDigest };
          currentOutcome = { ...proposedOutcome, job_version: 4, adopted_revision_id: graphId };
          throw new Error('Connection lost after the server saved adoption');
        }
        return receipt as T;
      }
      if (path.startsWith(`/epics/${epicId}/decomposition-conversations?`)) return [thread] as T;
      if (path.startsWith(`/epics/${epicId}/decomposition-conversations/${conversationId}/turns?`)) return turns as T;
      if (path.startsWith(`/epics/${epicId}/decomposition-jobs/decomp-job-1?`)) return currentOutcome as T;
      if (path === `/epics/${epicId}`) return savedEpic as T;
      if (path === `/epics/${epicId}/brief-revisions`) return [] as T;
      if (path === `/epics/${epicId}/graph-revisions`) return (adoptionCount ? [graph] : []) as T;
      if (path === `/epics/${epicId}/accepted-graph`) return acceptedGraph as T;
      return undefined as T;
    });
    function GraphProjection() {
      const workspace = useEpicWorkspace(epicId);
      return <output data-testid="saved-graph">{workspace.epic?.version}:{workspace.acceptedGraph?.graph_revision_id}:{workspace.graphRevisions.length}</output>;
    }
    const workspace = <EpicWorkspaceProvider epicId={epicId}>
      <GraphProjection />
      <DecompositionWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />
    </EpicWorkspaceProvider>;
    const view = render(workspace);
    await userEvent.click(await screen.findByRole('button', { name: 'Adopt proposed decomposition' }));
    expect(await screen.findByText('The last request may still have completed.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send decomposition message' })).toBeDisabled();
    expect(adoptionRequests[0].body).toBe(JSON.stringify({
      schema_version: 1, project_id: projectId, expected_job_version: 3, expected_epic_version: 9, proposal_digest: proposedOutcome.proposal_digest,
    }));
    view.unmount();
    resetEpicMutationStoreForTesting();
    render(workspace);
    expect(await screen.findByText('The last request may still have completed.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send decomposition message' })).toBeDisabled();
    await waitFor(() => expect(screen.getByTestId('saved-graph')).toHaveTextContent(`10:${graphId}:1`));
    const graphReadsBeforeReplay = vi.mocked(api).mock.calls.filter(([path]) => path === `/epics/${epicId}/graph-revisions`).length;
    await userEvent.click(screen.getByRole('button', { name: 'Retry original request' }));
    await waitFor(() => expect(screen.queryByText('The last request may still have completed.')).not.toBeInTheDocument());
    expect(adoptionRequests).toHaveLength(2);
    expect(adoptionRequests[1]).toEqual(adoptionRequests[0]);
    expect(new Headers(adoptionRequests[0].headers).get('Idempotency-Key')).toBeTruthy();
    expect(adoptionCount).toBe(1);
    expect(await screen.findByText('This decomposition proposal has been adopted.')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('saved-graph')).toHaveTextContent(`10:${graphId}:1`));
    await waitFor(() => expect(vi.mocked(api).mock.calls.filter(([path]) => path === `/epics/${epicId}/graph-revisions`).length).toBeGreaterThan(graphReadsBeforeReplay));
    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/accepted-graph`, expect.anything());
  });

  test('saving a decomposition turn shows only the save request and never claims a job was submitted', async () => {
    let finishSave!: (value: unknown) => void;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/decomposition-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/decomposition-conversations/${conversationId}/turns?project_id=${projectId}`) return turns as T;
      if (path === `/epics/${epicId}/decomposition-jobs/decomp-job-1?project_id=${projectId}`) return proposedOutcome as T;
      if (path === `/epics/${epicId}/decomposition-conversations/${conversationId}/turns` && init?.method === 'POST') return await new Promise<unknown>(resolve => { finishSave = resolve; }) as T;
      if (path.startsWith(`/epics/${epicId}/brief-revisions`) || path.startsWith(`/epics/${epicId}/graph-revisions`)) return [] as T;
      if (path === `/epics/${epicId}`) return { epic_id: epicId, version: 9, project_id: projectId } as T;
      return undefined as T;
    });
    render(<DecompositionWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.type(await screen.findByLabelText('Decomposition message or instruction'), 'One more constraint');
    await userEvent.click(screen.getByRole('button', { name: 'Send decomposition message' }));
    expect(screen.getByText('Saving decomposition message')).toBeInTheDocument();
    expect(screen.queryByText('Submitting prompt')).not.toBeInTheDocument();
    expect(vi.mocked(api).mock.calls.some(([path]) => path === `/epics/${epicId}/decomposition-conversations/${conversationId}/jobs`)).toBe(false);
    await act(async () => finishSave({ version: 3 }));
  });

  test('accepted decomposition receipt remains visible while its first outcome read is delayed', async () => {
    let finishOutcome!: (value: AuthoringOutcome) => void;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.startsWith(`/epics/${epicId}/decomposition-conversations?`)) return [thread] as T;
      if (path === `/epics/${epicId}/decomposition-conversations/${conversationId}/turns?project_id=${projectId}`) return turns as T;
      if (path === `/epics/${epicId}/decomposition-jobs/decomp-job-1?project_id=${projectId}`) return proposedOutcome as T;
      if (path === `/epics/${epicId}/decomposition-conversations/${conversationId}/jobs` && init?.method === 'POST') return { schema_version: 1, job_id: 'new-decomp-job', job_version: 1, state: 'queued', replay_key: 'rk' } as T;
      if (path === `/epics/${epicId}/decomposition-jobs/new-decomp-job?project_id=${projectId}`) return await new Promise<unknown>(resolve => { finishOutcome = resolve; }) as T;
      if (path.startsWith(`/epics/${epicId}/brief-revisions`) || path.startsWith(`/epics/${epicId}/graph-revisions`)) return [] as T;
      if (path === `/epics/${epicId}`) return { epic_id: epicId, version: 9, project_id: projectId } as T;
      return undefined as T;
    });
    render(<DecompositionWorkspace epicId={epicId} projectId={projectId} epicVersion={9} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Generate proposed decomposition' }));
    const checking = await screen.findByRole('region', { name: /Job submitted · checking assistant status/i });
    expect(checking).toHaveAttribute('data-executing', 'true');
    expect(screen.queryByText('Proposal ready for review')).not.toBeInTheDocument();
    await act(async () => finishOutcome({ ...proposedOutcome, job_id: 'new-decomp-job', job_version: 1, state: 'queued', proposal: null, proposal_digest: null }));
    expect(await screen.findByText('Assistant job queued')).toBeInTheDocument();
  });
});
