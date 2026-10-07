import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { DeliveryWorkspace } from './delivery-workspace';
import { api, ApiError } from '@/lib/api/client';
import { useEpicWorkspace } from '@/hooks/epics/use-epic-workspace';
import type { EpicExecutionProjection, GraphRevisionResponse, BriefRevisionResponse } from '@/hooks/epics/types';

vi.mock('@/lib/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/client')>('@/lib/api/client');
  return {
    ...actual,
    api: vi.fn(),
  };
});
vi.mock('@/hooks/epics/use-epic-workspace', () => ({ useEpicWorkspace: vi.fn() }));

describe('DeliveryWorkspace', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';
  const executionId = '22222222-2222-4222-8222-222222222222';
  const briefRevId = '33333333-3333-4333-8333-333333333333';
  const graphRevId = '44444444-4444-4444-8444-444444444444';
  const item1Id = '66666666-6666-4666-8666-666666666666';
  const item2Id = '55555555-5555-4555-8555-555555555555';
  const item3Id = '77777777-7777-4777-8777-777777777777';
  const run1Id = '88888888-8888-4888-8888-888888888888';

  const defaultProjection: EpicExecutionProjection = {
    execution: {
      epic_id: epicId,
      execution_id: executionId,
      brief_revision_id: briefRevId,
      brief_digest: 'b'.repeat(64),
      graph_revision_id: graphRevId,
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
          brief_revision_id: briefRevId,
          context_digest: 'c'.repeat(64),
          created_at: '2026-10-01T00:00:00Z',
          dependency_evidence: [],
          epic_id: epicId,
          execution_id: executionId,
          expected_epic_version: 7,
          graph_digest: 'g'.repeat(64),
          graph_revision_id: graphRevId,
          item_digest: 'i'.repeat(64),
          item_disposition: 'required',
          item_id: item1Id,
          override_note: null,
          owner_override: false,
          run_id: run1Id,
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
  };

  const frozenGraph: GraphRevisionResponse = {
    schema_version: 1,
    epic_id: epicId,
    epic_version: 7,
    brief_revision_id: briefRevId,
    brief_digest: 'b'.repeat(64),
    revision_number: 2,
    created_at: '2026-10-01T00:00:00Z',
    graph_revision_id: graphRevId,
    graph_digest: 'g'.repeat(64),
    items: [
      {
        item_id: item1Id,
        graph_revision_id: graphRevId,
        item_digest: '1'.repeat(64),
        title: 'Frozen authentication delivery',
        outcome: 'Secure sessions ship safely',
        disposition: 'required',
        ordinal: 0,
        source_requirement_ids: [],
        dependency_item_ids: [],
        acceptance_criteria: ['The frozen acceptance check passes.'],
      },
      {
        item_id: item2Id,
        graph_revision_id: graphRevId,
        item_digest: '2'.repeat(64),
        title: 'Frozen integration check',
        outcome: 'The upstream result is verified',
        disposition: 'required',
        ordinal: 1,
        source_requirement_ids: [],
        dependency_item_ids: [],
        acceptance_criteria: ['The integration passes.'],
      },
      {
        item_id: item3Id,
        graph_revision_id: graphRevId,
        item_digest: '3'.repeat(64),
        title: 'Frozen deferred cleanup',
        outcome: 'Cleanup follows later',
        disposition: 'deferred',
        ordinal: 2,
        source_requirement_ids: [],
        dependency_item_ids: [],
        acceptance_criteria: ['Cleanup passes.'],
      },
    ],
  };

  const alternateBrief: BriefRevisionResponse = {
    schema_version: 1,
    epic_id: epicId,
    epic_version: 7,
    brief_revision_id: 'alt-brief-id',
    content_digest: 'altbriefdigest'.repeat(4).slice(0, 64),
    revision_number: 3,
    created_at: '2026-10-02T00:00:00Z',
    source_job_id: null,
    content: {
      schema_version: 1,
      problem: 'Alternate problem',
      requirements: [],
    },
  };

  const alternateGraph: GraphRevisionResponse = {
    schema_version: 1,
    epic_id: epicId,
    epic_version: 7,
    brief_revision_id: 'alt-brief-id',
    brief_digest: 'altbriefdigest'.repeat(4).slice(0, 64),
    revision_number: 3,
    created_at: '2026-10-02T00:00:00Z',
    graph_revision_id: 'alt-graph-id',
    graph_digest: 'altgraphdigest'.repeat(4).slice(0, 64),
    items: [],
  };

  beforeEach(() => {
    resetEpicMutationStoreForTesting();
    vi.clearAllMocks();
    sessionStorage.clear();
    window.history.replaceState({}, '', `/epics/${epicId}`);
    vi.mocked(useEpicWorkspace).mockReturnValue({
      briefRevisions: [alternateBrief],
      graphRevisions: [frozenGraph, alternateGraph],
      acceptedBrief: {
        brief_revision_id: briefRevId,
        brief_digest: 'b'.repeat(64),
      },
      acceptedGraph: {
        ...frozenGraph,
        graph_revision_id: graphRevId,
        graph_digest: 'g'.repeat(64),
        items: [{ item_id: item1Id, title: 'Current accepted label' }],
      },
    } as unknown as ReturnType<typeof useEpicWorkspace>);
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if ((!init?.method || init.method === 'GET') && path.endsWith('/executions')) {
        return [] as T;
      }
      if ((!init?.method || init.method === 'GET') && path.endsWith('/budget')) {
        return {
          schema_version: 1,
          budget_policy_id: 'bp-1',
          budget_policy_version: 1,
          scope_type: 'epic',
          scope_id: epicId,
          version: 1,
          created_at: '2026-10-01T00:00:00Z',
          updated_at: '2026-10-01T00:00:00Z',
          task_budget: {
            max_duration_seconds: 1800,
            max_cost_minor: 50,
            max_input_tokens: 100000,
            max_output_tokens: 20000,
            max_tool_calls: 30,
            max_provider_attempts: 3,
            disabled_dimensions: [],
          },
        } as T;
      }
      return undefined as T;
    });
  });

  afterEach(() => {
    resetEpicMutationStoreForTesting();
    cleanup();
    vi.mocked(api).mockReset();
    sessionStorage.clear();
  });

  test('explains discovery when no executions exist and offers start and ID lookup', async () => {
    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    expect(await screen.findByText(/No executions discovered for this epic yet/)).toBeInTheDocument();
    expect(screen.getByLabelText('Execution ID')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start Execution' })).toBeInTheDocument();
  });

  test('renders frozen state, child gate, and real item details from frozen graph', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) {
        return defaultProjection as T;
      }
      if ((!init?.method || init.method === 'GET') && path.endsWith('/budget')) {
        return {
          schema_version: 1,
          budget_policy_id: 'bp-1',
          budget_policy_version: 1,
          scope_type: 'epic',
          scope_id: epicId,
          version: 1,
          created_at: '2026-10-01T00:00:00Z',
          updated_at: '2026-10-01T00:00:00Z',
          task_budget: {
            max_duration_seconds: 1800,
            max_cost_minor: 50,
            max_input_tokens: 100000,
            max_output_tokens: 20000,
            max_tool_calls: 30,
            max_provider_attempts: 3,
            disabled_dimensions: [],
          },
        } as T;
      }
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);

    expect(await screen.findByText('ACTIVE')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Run for Frozen authentication delivery' })).toHaveAttribute('href', `/runs/${run1Id}`);
    expect(screen.getAllByText(/Frozen authentication delivery/).length).toBeGreaterThan(0);
    expect(screen.queryByText('Current accepted label')).not.toBeInTheDocument();
    expect(screen.getByText(/run version 7/i)).toBeInTheDocument();
    await userEvent.click(screen.getByText('Inspect frozen brief and graph revisions'));
    expect(screen.getByText(`brief_revision_id: ${briefRevId}`)).toBeInTheDocument();
    expect(screen.getByText(`graph_revision_id: ${graphRevId}`)).toBeInTheDocument();
    expect(screen.getByText('plan')).toBeInTheDocument();
    expect(screen.getAllByText('deferred').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Pause Execution' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Resume Execution' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel Execution' })).toBeEnabled();
  });

  test('keeps requested transitions pending and permits only state-appropriate controls', async () => {
    const requested: EpicExecutionProjection = { ...defaultProjection, control_state: 'PAUSE_REQUESTED' };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return requested as T;
      if (path === `/epics/${epicId}/executions/${executionId}/commands` && init?.method === 'POST') {
        return { schema_version: 1, action: 'cancel', execution_version: 4, state: 'CANCEL_REQUESTED' } as T;
      }
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    expect(await screen.findByText('PAUSE REQUESTED')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Pause Execution' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Resume Execution' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel Execution' })).toBeDisabled();
    expect(screen.getByText(/requested change is still being processed/)).toBeInTheDocument();
  });

  test('retains the execution URL after start', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions` && init?.method === 'POST') {
        return { schema_version: 1, execution_id: executionId, execution_version: 1, state: 'ACTIVE' } as T;
      }
      if (path === `/epics/${epicId}/executions/${executionId}`) return defaultProjection as T;
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    await waitFor(() => expect(new URL(window.location.href).searchParams.get('execution_id')).toBe(executionId));
    expect(await screen.findByRole('status')).toHaveTextContent('Execution started; loading frozen progress.');
    expect(await screen.findByText('ACTIVE')).toBeInTheDocument();
  });

  test('reports a command as requested until the next server projection confirms it', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return defaultProjection as T;
      if (path === `/epics/${epicId}/executions/${executionId}/commands` && init?.method === 'POST') {
        return { schema_version: 1, action: 'pause', execution_version: 4, state: 'PAUSE_REQUESTED' } as T;
      }
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Pause Execution' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Pause requested; waiting for server confirmation.');
    expect(screen.getByText('ACTIVE')).toBeInTheDocument();
  });

  test('keeps the child link and gate readable and wrapping at mobile widths', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return defaultProjection as T;
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    const runLink = await screen.findByRole('link', { name: 'Run for Frozen authentication delivery' });
    expect(runLink).toHaveAttribute('href', `/runs/${run1Id}`);
    expect(runLink.parentElement).toHaveClass('flex-wrap');
    expect(screen.getAllByText('AWAITING PLAN APPROVAL').length).toBeGreaterThan(0);
    expect(screen.getByRole('link', { name: 'Review Gate at plan' })).toHaveAttribute('href', `/runs/${run1Id}`);
  });

  test.each([
    { state: 'PAUSED' as const, resume: true, cancel: true, next: /the execution is paused/i },
    { state: 'CANCEL_REQUESTED' as const, resume: false, cancel: false, next: /the requested change is still being processed/i },
    { state: 'CANCELLED' as const, resume: false, cancel: false, next: /the execution is cancelled/i },
    { state: 'BLOCKED' as const, resume: true, cancel: true, next: /resolve the reported blockers/i },
    { state: 'SUCCEEDED' as const, resume: false, cancel: false, next: /execution completed \(status succeeded\)/i },
  ])('$state derives lifecycle controls truthfully', async ({ state, resume, cancel, next }) => {
    const proj: EpicExecutionProjection = { ...defaultProjection, control_state: state };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return proj as T;
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    expect(await screen.findByText(state.replaceAll('_', ' '))).toBeInTheDocument();
    expect(screen.getByText(next)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Run for Frozen authentication delivery/ })).toHaveAttribute('href', `/runs/${run1Id}`);
    const resumeButton = screen.getByRole('button', { name: 'Resume Execution' });
    const cancelButton = screen.getByRole('button', { name: 'Cancel Execution' });
    if (resume) expect(resumeButton).toBeEnabled(); else expect(resumeButton).toBeDisabled();
    if (cancel) expect(cancelButton).toBeEnabled(); else expect(cancelButton).toBeDisabled();
    expect(screen.getAllByText('deferred').length).toBeGreaterThan(0);
    if (state === 'SUCCEEDED') {
      expect(screen.getByText('Execution completed with status: SUCCEEDED.')).toBeInTheDocument();
    }
  });

  test.each(['plan', 'pr', 'merge'] as const)('preserves the %s approval link through progress controls', async gate => {
    const proj: EpicExecutionProjection = {
      ...defaultProjection,
      children: [
        {
          ...defaultProjection.children[0],
          pending_gate: gate,
        },
      ],
    };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return proj as T;
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    expect(await screen.findByRole('link', { name: `Review Gate at ${gate}` })).toHaveAttribute('href', `/runs/${run1Id}`);
  });

  test('displays retained_gate when pending_gate is null for a paused child and current pending_gate when resumed', async () => {
    const pausedProjection: EpicExecutionProjection = {
      ...defaultProjection,
      control_state: 'PAUSED',
      children: [
        {
          ...defaultProjection.children[0],
          run_state: 'PAUSED',
          pending_gate: null,
          retained_gate: 'plan',
          effects_settled: false,
        },
      ],
    };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return pausedProjection as T;
      return undefined as T;
    });
    const { unmount } = render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);

    expect(await screen.findByText('plan (retained)')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Review Gate at plan' })).toHaveAttribute('href', `/runs/${run1Id}`);
    expect(screen.getByText('Effects unsettled')).toBeInTheDocument();
    unmount();

    // Now resumed projection restores pending_gate
    const resumedProjection: EpicExecutionProjection = {
      ...defaultProjection,
      control_state: 'ACTIVE',
      children: [
        {
          ...defaultProjection.children[0],
          run_state: 'IMPLEMENTING',
          pending_gate: 'plan',
          retained_gate: null,
          effects_settled: true,
        },
      ],
    };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return resumedProjection as T;
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);

    expect(await screen.findByText('plan')).toBeInTheDocument();
    expect(screen.queryByText('plan (retained)')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Review Gate at plan' })).toHaveAttribute('href', `/runs/${run1Id}`);
    expect(screen.getByText('Effects settled')).toBeInTheDocument();
  });

  test('disables control buttons for honest legacy execution when control_version is null', async () => {
    const legacyProjection: EpicExecutionProjection = {
      ...defaultProjection,
      control_version: null,
      control_state: null,
    };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return legacyProjection as T;
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);

    expect(await screen.findByText('Legacy (control unavailable)')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Pause Execution' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Resume Execution' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel Execution' })).toBeDisabled();
  });

  test('permits pause and resume from BLOCKED for owner recovery', async () => {
    const blockedProjection: EpicExecutionProjection = {
      ...defaultProjection,
      control_state: 'BLOCKED',
      blocker_code: 'child_execution_failed',
    };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return blockedProjection as T;
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);

    expect(await screen.findByText('BLOCKED')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Pause Execution' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Resume Execution' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Cancel Execution' })).toBeEnabled();
  });

  test('offers Start New Execution button in discovered execution view and starts a second epoch', async () => {
    const discoveredList: EpicExecutionProjection[] = [defaultProjection];
    const secondExecutionId = '99999999-9999-4999-8999-999999999999';
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if ((!init?.method || init.method === 'GET') && path.endsWith('/executions')) {
        return discoveredList as T;
      }
      if (path === `/epics/${epicId}/executions/${executionId}`) {
        return defaultProjection as T;
      }
      if (path === `/epics/${epicId}/executions` && init?.method === 'POST') {
        return { schema_version: 1, execution_id: secondExecutionId, execution_version: 1, state: 'ACTIVE' } as T;
      }
      return undefined as T;
    });

    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    // Discovered execution is loaded
    expect(await screen.findByText('Discovered Executions:')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start New Execution' })).toBeInTheDocument();

    // Click "Start New Execution" to open epoch start form
    await userEvent.click(screen.getByRole('button', { name: 'Start New Execution' }));
    expect(screen.getByText('Start New Execution Epoch')).toBeInTheDocument();

    // Click "Start Execution" to start second epoch
    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    await waitFor(() => expect(new URL(window.location.href).searchParams.get('execution_id')).toBe(secondExecutionId));
  });

  test('starts an execution using alternate saved sources with owner override and note', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions` && init?.method === 'POST') {
        const body = JSON.parse(init.body as string);
        expect(body.owner_override).toBe(true);
        expect(body.override_note).toBe('Testing custom sources');
        expect(body.brief_revision_id).toBe('alt-brief-id');
        expect(body.graph_revision_id).toBe('alt-graph-id');
        return { schema_version: 1, execution_id: executionId, execution_version: 1, state: 'ACTIVE' } as T;
      }
      if (path === `/epics/${epicId}/executions/${executionId}`) return defaultProjection as T;
      return undefined as T;
    });

    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    // Toggle owner override
    await userEvent.click(screen.getByLabelText(/Owner Override: Select custom saved brief \/ graph sources/i));
    expect(screen.getByText(/Warning: Starting execution with non-default or non-accepted sources/i)).toBeInTheDocument();

    // Select alternate brief and graph
    await userEvent.selectOptions(screen.getByLabelText('Brief Revision'), 'alt-brief-id');
    await userEvent.selectOptions(screen.getByLabelText('Graph Revision'), 'alt-graph-id');
    await userEvent.type(screen.getByLabelText('Override Note (optional)'), 'Testing custom sources');

    // Submit start
    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    await waitFor(() => expect(new URL(window.location.href).searchParams.get('execution_id')).toBe(executionId));
  });

  test('renders intents and refusals, and owner actions with warnings', async () => {
    const projectionWithAudit: EpicExecutionProjection = {
      ...defaultProjection,
      intents: [
        {
          intent_id: 'int-1',
          action: 'pause',
          control_version: 3,
          command_id: 'cmd-1',
          run_id: run1Id,
          refusal: 'Child run att-1 busy with uninterruptible step',
          status: 'refused',
        },
      ],
      owner_actions: [
        {
          actor_id: 'operator-1',
          event_type: 'execution_started',
          note: 'Operator started non-standard execution',
          warnings: ['Non-accepted brief source selected'],
        },
      ],
    };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return projectionWithAudit as T;
      if ((!init?.method || init.method === 'GET') && path.endsWith('/budget')) {
        return {
          schema_version: 1,
          budget_policy_id: 'bp-1',
          budget_policy_version: 1,
          scope_type: 'epic',
          scope_id: epicId,
          version: 1,
          created_at: '2026-10-01T00:00:00Z',
          updated_at: '2026-10-01T00:00:00Z',
          task_budget: {
            max_duration_seconds: 1800,
            max_cost_minor: 50,
            max_input_tokens: 100000,
            max_output_tokens: 20000,
            max_tool_calls: 30,
            max_provider_attempts: 3,
            disabled_dimensions: [],
          },
        } as T;
      }
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);

    expect(await screen.findByText('Recent Control Intents & Refusals')).toBeInTheDocument();
    expect(screen.getByText(/Child run att-1 busy with uninterruptible step/)).toBeInTheDocument();
    expect(screen.getByText('Owner Actions & Audit Warnings')).toBeInTheDocument();
    expect(screen.getByText(/Operator started non-standard execution/)).toBeInTheDocument();
    expect(screen.getByText('Non-accepted brief source selected')).toBeInTheDocument();
  });

  test('does not use the current graph when the frozen historical projection is missing', async () => {
    vi.mocked(useEpicWorkspace).mockReturnValue({
      graphRevisions: [],
      acceptedGraph: { ...frozenGraph, graph_revision_id: 'new-current-graph', items: [{ item_id: item1Id, title: 'Current accepted label' }] },
    } as unknown as ReturnType<typeof useEpicWorkspace>);
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return defaultProjection as T;
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    expect(await screen.findByText(/Details for this frozen graph are unavailable/)).toBeInTheDocument();
    expect(screen.queryByText('Current accepted label')).not.toBeInTheDocument();
  });

  test('keeps all controls locked after an uncertain execution start', async () => {
    vi.mocked(api).mockRejectedValue(new Error('connection lost'));
    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    await screen.findByText('Retry original request');
    expect(screen.getByRole('button', { name: 'Start Execution' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Load Execution' })).toBeDisabled();
    expect(screen.getByLabelText('Execution ID')).toBeDisabled();
  });

  test('shows a definitive missing-producer rejection', async () => {
    vi.mocked(api).mockRejectedValue(new ApiError(404, 'not-found'));
    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    expect(await screen.findByText(/action is unavailable or was rejected/i)).toBeInTheDocument();
  });

  test('distinguishes execution-command version conflict from execution-start epic version conflict', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return defaultProjection as T;
      if (path === `/epics/${epicId}/executions/${executionId}/commands` && init?.method === 'POST') throw new ApiError(409, 'version-conflict');
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    expect(await screen.findByText('ACTIVE')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Pause Execution' }));
    expect(await screen.findByText('Conflict: Execution version has changed concurrently.')).toBeInTheDocument();
  });

  test('shows epic version conflict on execution-start 409', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions` && init?.method === 'POST') throw new ApiError(409, 'version-conflict');
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    expect(await screen.findByText('Conflict: The epic version changed on the server before starting execution.')).toBeInTheDocument();
  });

  test('selecting graph alone binds its actual brief revision and submits coherent pair', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions` && init?.method === 'POST') {
        const body = JSON.parse(init.body as string);
        expect(body.owner_override).toBe(true);
        expect(body.brief_revision_id).toBe('alt-brief-id');
        expect(body.graph_revision_id).toBe('alt-graph-id');
        return { schema_version: 1, execution_id: executionId, execution_version: 1, state: 'ACTIVE' } as T;
      }
      if (path === `/epics/${epicId}/executions/${executionId}`) return defaultProjection as T;
      return undefined as T;
    });

    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    await userEvent.click(screen.getByLabelText(/Owner Override: Select custom saved brief \/ graph sources/i));

    // Select Graph Revision alone
    const graphSelect = screen.getByLabelText('Graph Revision');
    await userEvent.selectOptions(graphSelect, 'alt-graph-id');

    // Selecting graph alone must bind its actual brief revision!
    const briefSelect = screen.getByLabelText('Brief Revision');
    expect(briefSelect).toHaveValue('alt-brief-id');

    // Submitting start must send the coherent pair bound to the graph
    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    await waitFor(() => expect(new URL(window.location.href).searchParams.get('execution_id')).toBe(executionId));
  });

  test('selecting brief alone offers and selects matching graphs and submits coherent pair', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions` && init?.method === 'POST') {
        const body = JSON.parse(init.body as string);
        expect(body.owner_override).toBe(true);
        expect(body.brief_revision_id).toBe('alt-brief-id');
        expect(body.graph_revision_id).toBe('alt-graph-id');
        return { schema_version: 1, execution_id: executionId, execution_version: 1, state: 'ACTIVE' } as T;
      }
      if (path === `/epics/${epicId}/executions/${executionId}`) return defaultProjection as T;
      return undefined as T;
    });

    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    await userEvent.click(screen.getByLabelText(/Owner Override: Select custom saved brief \/ graph sources/i));

    // Select Brief Revision alone
    const briefSelect = screen.getByLabelText('Brief Revision');
    await userEvent.selectOptions(briefSelect, 'alt-brief-id');

    // Selecting brief must offer/select matching graphs
    const graphSelect = screen.getByLabelText('Graph Revision');
    expect(graphSelect).toHaveValue('alt-graph-id');

    // Submitting start sends coherent pair
    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    await waitFor(() => expect(new URL(window.location.href).searchParams.get('execution_id')).toBe(executionId));
  });

  test('offers multiple draft graph revisions for a brief and binds chosen pair', async () => {
    const secondAlternateGraph: GraphRevisionResponse = {
      ...alternateGraph,
      graph_revision_id: 'alt-graph-2',
      graph_digest: 'altgraphdigest2'.repeat(4).slice(0, 64),
      revision_number: 4,
    };

    vi.mocked(useEpicWorkspace).mockReturnValue({
      briefRevisions: [alternateBrief],
      graphRevisions: [frozenGraph, alternateGraph, secondAlternateGraph],
      acceptedBrief: {
        brief_revision_id: briefRevId,
        brief_digest: 'b'.repeat(64),
      },
      acceptedGraph: {
        ...frozenGraph,
        graph_revision_id: graphRevId,
        graph_digest: 'g'.repeat(64),
        items: [{ item_id: item1Id, title: 'Current accepted label' }],
      },
    } as unknown as ReturnType<typeof useEpicWorkspace>);

    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions` && init?.method === 'POST') {
        const body = JSON.parse(init.body as string);
        expect(body.brief_revision_id).toBe('alt-brief-id');
        expect(body.graph_revision_id).toBe('alt-graph-2');
        return { schema_version: 1, execution_id: executionId, execution_version: 1, state: 'ACTIVE' } as T;
      }
      if (path === `/epics/${epicId}/executions/${executionId}`) return defaultProjection as T;
      return undefined as T;
    });

    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    await userEvent.click(screen.getByLabelText(/Owner Override: Select custom saved brief \/ graph sources/i));

    // Select Brief Revision
    await userEvent.selectOptions(screen.getByLabelText('Brief Revision'), 'alt-brief-id');

    // Graph select should offer both matching graphs
    const graphSelect = screen.getByLabelText('Graph Revision') as HTMLSelectElement;
    const optionValues = Array.from(graphSelect.options).map(o => o.value);
    expect(optionValues).toContain('alt-graph-id');
    expect(optionValues).toContain('alt-graph-2');

    // Choose the second graph
    await userEvent.selectOptions(graphSelect, 'alt-graph-2');

    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    await waitFor(() => expect(new URL(window.location.href).searchParams.get('execution_id')).toBe(executionId));
  });

  test('shows clear feedback and prevents invalid POST when brief has no matching graph', async () => {
    const orphanBrief: BriefRevisionResponse = {
      ...alternateBrief,
      brief_revision_id: 'orphan-brief-id',
      revision_number: 99,
    };

    vi.mocked(useEpicWorkspace).mockReturnValue({
      briefRevisions: [alternateBrief, orphanBrief],
      graphRevisions: [frozenGraph, alternateGraph], // No graph matching orphan-brief-id
      acceptedBrief: {
        brief_revision_id: briefRevId,
        brief_digest: 'b'.repeat(64),
      },
      acceptedGraph: {
        ...frozenGraph,
        graph_revision_id: graphRevId,
        graph_digest: 'g'.repeat(64),
        items: [{ item_id: item1Id, title: 'Current accepted label' }],
      },
    } as unknown as ReturnType<typeof useEpicWorkspace>);

    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    await userEvent.click(screen.getByLabelText(/Owner Override: Select custom saved brief \/ graph sources/i));

    // Select orphan brief
    await userEvent.selectOptions(screen.getByLabelText('Brief Revision'), 'orphan-brief-id');

    // Should display clear feedback that no matching graph revision is available
    expect(await screen.findByText(/No matching graph revision available for this brief revision/i)).toBeInTheDocument();

    // Clicking Start Execution must not POST
    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    const postCalls = vi.mocked(api).mock.calls.filter(([p, init]) => p === `/epics/${epicId}/executions` && init?.method === 'POST');
    expect(postCalls).toHaveLength(0);
  });

  test('uses accepted graph bindings when the separate brief read is unavailable', async () => {
    vi.mocked(useEpicWorkspace).mockReturnValue({
      briefRevisions: [],
      graphRevisions: [],
      acceptedBrief: undefined,
      acceptedGraph: frozenGraph,
    } as unknown as ReturnType<typeof useEpicWorkspace>);
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions` && init?.method === 'POST') {
        return { schema_version: 1, execution_id: executionId, execution_version: 1, state: 'ACTIVE' } as T;
      }
      if (path === `/epics/${epicId}/executions/${executionId}`) return defaultProjection as T;
      return undefined as T;
    });

    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    await userEvent.click(screen.getByLabelText(/Owner Override: Select custom saved brief \/ graph sources/i));
    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    await waitFor(() => {
      const calls = vi.mocked(api).mock.calls.filter(([path, init]) => path === `/epics/${epicId}/executions` && init?.method === 'POST');
      expect(calls).toHaveLength(1);
      expect(JSON.parse(calls[0][1]!.body as string)).toMatchObject({
        brief_revision_id: frozenGraph.brief_revision_id,
        brief_digest: frozenGraph.brief_digest,
        graph_revision_id: frozenGraph.graph_revision_id,
        graph_digest: frozenGraph.graph_digest,
        owner_override: true,
      });
    });
  });

  test('requires reselection when a graph disappears after refresh', async () => {
    const source = {
      briefRevisions: [alternateBrief],
      graphRevisions: [frozenGraph, alternateGraph],
      acceptedBrief: { brief_revision_id: briefRevId, brief_digest: frozenGraph.brief_digest },
      acceptedGraph: frozenGraph,
    } as unknown as ReturnType<typeof useEpicWorkspace>;
    vi.mocked(useEpicWorkspace).mockReturnValue(source);
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions` && init?.method === 'POST') {
        return { schema_version: 1, execution_id: executionId, execution_version: 1, state: 'ACTIVE' } as T;
      }
      if (path === `/epics/${epicId}/executions/${executionId}`) return defaultProjection as T;
      return undefined as T;
    });
    const { rerender } = render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    await userEvent.click(screen.getByLabelText(/Owner Override: Select custom saved brief \/ graph sources/i));
    await userEvent.selectOptions(screen.getByLabelText('Graph Revision'), alternateGraph.graph_revision_id);

    const replacement = { ...alternateGraph, graph_revision_id: 'replacement-graph', graph_digest: 'r'.repeat(64), revision_number: 4 };
    vi.mocked(useEpicWorkspace).mockReturnValue({ ...source, graphRevisions: [frozenGraph, replacement] });
    rerender(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Selected graph revision is unavailable. Choose a saved graph revision.');
    expect(screen.getByRole('button', { name: 'Start Execution' })).toBeDisabled();
    expect(vi.mocked(api).mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0);

    await userEvent.selectOptions(screen.getByLabelText('Graph Revision'), replacement.graph_revision_id);
    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    await waitFor(() => {
      const calls = vi.mocked(api).mock.calls.filter(([path, init]) => path === `/epics/${epicId}/executions` && init?.method === 'POST');
      expect(calls).toHaveLength(1);
      expect(JSON.parse(calls[0][1]!.body as string)).toMatchObject({
        brief_revision_id: alternateBrief.brief_revision_id,
        brief_digest: alternateBrief.content_digest,
        graph_revision_id: replacement.graph_revision_id,
        graph_digest: replacement.graph_digest,
      });
    });
  });

  test('truthfully displays current epic version without claiming frozen execution was bound to it across rerender', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return defaultProjection as T;
      return undefined as T;
    });

    const { rerender } = render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);

    // Truthful label: Current Epic Version 7, never "Bound to Epic Version"
    expect(await screen.findByText(/Current Epic Version 7/i)).toBeInTheDocument();
    expect(screen.queryByText(/Bound to Epic Version/i)).not.toBeInTheDocument();

    // Frozen brief/graph identity is displayed in EvidenceDetails
    await userEvent.click(screen.getByText('Inspect frozen brief and graph revisions'));
    expect(screen.getByText(`brief_revision_id: ${briefRevId}`)).toBeInTheDocument();
    expect(screen.getByText(`graph_revision_id: ${graphRevId}`)).toBeInTheDocument();

    // Rerender with changed current epicVersion (e.g. 9)
    rerender(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={9} />);

    // Shows updated current epic version truthfully
    expect(await screen.findByText(/Current Epic Version 9/i)).toBeInTheDocument();
    // Does NOT claim old frozen execution was bound to epic version 9
    expect(screen.queryByText(/Bound to Epic Version/i)).not.toBeInTheDocument();

    // Actual frozen IDs remain unchanged
    expect(screen.getByText(`brief_revision_id: ${briefRevId}`)).toBeInTheDocument();
    expect(screen.getByText(`graph_revision_id: ${graphRevId}`)).toBeInTheDocument();
  });
});
