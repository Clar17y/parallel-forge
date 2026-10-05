import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { DeliveryWorkspace } from './delivery-workspace';
import { api, ApiError } from '@/lib/api/client';
import { useEpicWorkspace } from '@/hooks/epics/use-epic-workspace';
import type { ExecutionProgress, GraphRevisionResponse } from '@/hooks/epics/types';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) { super(code); }
  },
}));
vi.mock('@/hooks/epics/use-epic-workspace', () => ({ useEpicWorkspace: vi.fn() }));

describe('DeliveryWorkspace', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';
  const executionId = '22222222-2222-4222-8222-222222222222';
  const frozenProgress: ExecutionProgress = {
    schema_version: 1,
    epic_id: epicId,
    epic_version: 7,
    execution_id: executionId,
    execution_version: 3,
    state: 'ACTIVE',
    brief_revision_id: '33333333-3333-4333-8333-333333333333',
    graph_revision_id: '44444444-4444-4444-8444-444444444444',
    active_child: {
      item_id: '66666666-6666-4666-8666-666666666666',
      run_id: '88888888-8888-4888-8888-888888888888',
      run_version: 7,
      run_state: 'AWAITING_PLAN_APPROVAL',
      pending_gate: 'plan',
      pending_evidence_digest: 'f'.repeat(64),
    },
    items: [
      { item_id: '66666666-6666-4666-8666-666666666666', disposition: 'required', status: 'active', blocker_code: null, run_id: '88888888-8888-4888-8888-888888888888' },
      { item_id: '55555555-5555-4555-8555-555555555555', disposition: 'required', status: 'blocked', blocker_code: 'predecessor_integration_unverified', run_id: null },
      { item_id: '77777777-7777-4777-8777-777777777777', disposition: 'deferred', status: 'deferred', blocker_code: null, run_id: null },
    ],
    aggregate_usage: { known_cost_minor: 12, reserved_cost_minor: 40, unknown_usage: true },
  };
  const frozenGraph: GraphRevisionResponse = {
    schema_version: 1,
    epic_id: epicId,
    epic_version: frozenProgress.epic_version,
    brief_revision_id: frozenProgress.brief_revision_id,
    brief_digest: 'b'.repeat(64),
    revision_number: 2,
    created_at: '2026-10-01T00:00:00Z',
    graph_revision_id: frozenProgress.graph_revision_id,
    graph_digest: 'a'.repeat(64),
    items: frozenProgress.items.map((item, ordinal) => ({
      item_id: item.item_id,
      graph_revision_id: frozenProgress.graph_revision_id,
      item_digest: `${ordinal}`.repeat(64),
      title: ordinal === 0 ? 'Frozen authentication delivery' : ordinal === 1 ? 'Frozen integration check' : 'Frozen deferred cleanup',
      outcome: ordinal === 0 ? 'Secure sessions ship safely' : ordinal === 1 ? 'The upstream result is verified' : 'Cleanup follows later',
      disposition: item.disposition,
      ordinal,
      source_requirement_ids: [],
      dependency_item_ids: [],
      acceptance_criteria: ['The frozen acceptance check passes.'],
    })),
  };

  beforeEach(() => { resetEpicMutationStoreForTesting();
    vi.clearAllMocks();
    sessionStorage.clear();
    window.history.replaceState({}, '', `/epics/${epicId}`);
    vi.mocked(useEpicWorkspace).mockReturnValue({ graphRevisions: [frozenGraph], acceptedGraph: { graph_revision_id: 'new-current-graph', items: [{ item_id: frozenProgress.items[0].item_id, title: 'Current accepted label' }] } } as unknown as ReturnType<typeof useEpicWorkspace>);
  });
  afterEach(() => { resetEpicMutationStoreForTesting(); cleanup(); vi.mocked(api).mockReset(); sessionStorage.clear(); });

  test('explains that execution IDs come from an explicit start or operator lookup', () => {
    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    expect(screen.getByText(/Execution discovery is unavailable until an execution ID is supplied or returned by a start request/)).toBeInTheDocument();
    expect(screen.getByLabelText('Execution ID')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start Execution' })).toBeInTheDocument();
  });

  test('renders frozen state, child gate, item blockers/dispositions and known, reserved, unknown usage', async () => {
    vi.mocked(api).mockResolvedValueOnce(frozenProgress);
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);

    expect(await screen.findByText('ACTIVE')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Run for Frozen authentication delivery' })).toHaveAttribute('href', `/runs/${frozenProgress.active_child?.run_id}`);
    expect(screen.getAllByText(/Frozen authentication delivery/).length).toBeGreaterThan(0);
    expect(screen.queryByText('Current accepted label')).not.toBeInTheDocument();
    expect(screen.getByText(/run version 7/i)).toBeInTheDocument();
    await userEvent.click(screen.getByText('Inspect frozen brief and graph revisions'));
    expect(screen.getByText(`brief_revision_id: ${frozenProgress.brief_revision_id}`)).toBeInTheDocument();
    expect(screen.getByText(`graph_revision_id: ${frozenProgress.graph_revision_id}`)).toBeInTheDocument();
    expect(screen.getByText('plan')).toBeInTheDocument();
    expect(screen.getAllByText(/predecessor_integration_unverified/).length).toBeGreaterThan(0);
    expect(screen.getAllByText('deferred').length).toBeGreaterThan(0);
    expect(screen.getByText('12 minor units')).toBeInTheDocument();
    expect(screen.getByText('40 minor units')).toBeInTheDocument();
    expect(screen.getByText(/Unknown usage is not zero/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Pause Execution' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Resume Execution' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel Execution' })).toBeEnabled();
    expect(screen.queryByText(/Settled|Unsettled/)).not.toBeInTheDocument();
  });

  test('keeps requested transitions pending and permits only state-appropriate controls', async () => {
    const requested = { ...frozenProgress, state: 'PAUSE_REQUESTED' as const };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return requested as T;
      if (path === `/epics/${epicId}/executions/${executionId}/commands` && init?.method === 'POST') return { schema_version: 1, action: 'cancel', execution_version: 4, state: 'CANCEL_REQUESTED' } as T;
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
      if (path === `/epics/${epicId}/executions` && init?.method === 'POST') return { schema_version: 1, execution_id: executionId, execution_version: 1, state: 'ACTIVE' } as T;
      if (path === `/epics/${epicId}/executions/${executionId}`) return frozenProgress as T;
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    await waitFor(() => expect(new URL(window.location.href).searchParams.get('execution_id')).toBe(executionId));
    expect(await screen.findByRole('status')).toHaveTextContent('Execution started; loading frozen progress.');
    expect(screen.queryByText(`Execution started: ${executionId}`)).not.toBeInTheDocument();
    expect(await screen.findByText('ACTIVE')).toBeInTheDocument();
  });

  test('reports a command as requested until the next server projection confirms it', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}` && (!init?.method || init.method === 'GET')) return frozenProgress as T;
      if (path === `/epics/${epicId}/executions/${executionId}/commands` && init?.method === 'POST') return { schema_version: 1, action: 'pause', execution_version: 4, state: 'PAUSE_REQUESTED' } as T;
      return undefined as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Pause Execution' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Pause requested; waiting for server confirmation.');
    expect(screen.getByText('ACTIVE')).toBeInTheDocument();
  });

  test('keeps the child link and gate readable and wrapping at mobile widths', async () => {
    vi.mocked(api).mockResolvedValue(frozenProgress);
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    const runLink = await screen.findByRole('link', { name: 'Run for Frozen authentication delivery' });
    expect(runLink).toHaveAttribute('href', `/runs/${frozenProgress.active_child?.run_id}`);
    expect(runLink.parentElement).toHaveClass('flex-wrap');
    expect(runLink.parentElement?.parentElement?.parentElement).toHaveClass('flex-col');
    expect(screen.getByText('AWAITING PLAN APPROVAL')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Review Gate at plan' })).toHaveAttribute('href', `/runs/${frozenProgress.active_child?.run_id}`);
  });

  test.each([
    { state: 'PAUSED' as const, resume: true, cancel: true, next: /execution is paused/i },
    { state: 'CANCEL_REQUESTED' as const, resume: false, cancel: false, next: /requested change is still being processed/i },
    { state: 'CANCELLED' as const, resume: false, cancel: false, next: /execution is cancelled/i },
    { state: 'BLOCKED' as const, resume: false, cancel: true, next: /resolve the reported blockers/i },
    { state: 'SUCCEEDED' as const, resume: false, cancel: false, next: /all required work is verified complete/i },
  ])('$state retains frozen gates and derives lifecycle controls', async ({ state, resume, cancel, next }) => {
    vi.mocked(api).mockResolvedValue({ ...frozenProgress, state });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    expect(await screen.findByText(state.replaceAll('_', ' '))).toBeInTheDocument();
    expect(screen.getByText(next)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Run for Frozen authentication delivery/ })).toHaveAttribute('href', `/runs/${frozenProgress.active_child?.run_id}`);
    const resumeButton = screen.getByRole('button', { name: 'Resume Execution' });
    const cancelButton = screen.getByRole('button', { name: 'Cancel Execution' });
    if (resume) expect(resumeButton).toBeEnabled(); else expect(resumeButton).toBeDisabled();
    if (cancel) expect(cancelButton).toBeEnabled(); else expect(cancelButton).toBeDisabled();
    expect(screen.getAllByText('deferred').length).toBeGreaterThan(0);
    if (state === 'SUCCEEDED') expect(screen.getByText(/Overall Verified Success/)).toBeInTheDocument();
  });

  test.each(['plan', 'pr', 'merge'])('preserves the %s approval link through progress controls', async gate => {
    const progress = { ...frozenProgress, active_child: { ...frozenProgress.active_child!, pending_gate: gate } };
    vi.mocked(api).mockResolvedValue(progress);
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    expect(await screen.findByRole('link', { name: `Review Gate at ${gate}` })).toHaveAttribute('href', `/runs/${frozenProgress.active_child?.run_id}`);
  });

  test('does not use the current graph when the frozen historical projection is missing', async () => {
    vi.mocked(useEpicWorkspace).mockReturnValue({ graphRevisions: [], acceptedGraph: { graph_revision_id: 'new-current-graph', items: [{ item_id: frozenProgress.items[0].item_id, title: 'Current accepted label' }] } } as unknown as ReturnType<typeof useEpicWorkspace>);
    vi.mocked(api).mockResolvedValue(frozenProgress);
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    expect(await screen.findByText('Details for this frozen graph are unavailable.')).toBeInTheDocument();
    expect(screen.queryByText('Current accepted label')).not.toBeInTheDocument();
    expect(screen.getAllByText(/Work item details unavailable/).length).toBeGreaterThan(0);
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
    vi.mocked(api).mockResolvedValueOnce(frozenProgress);
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    expect(await screen.findByText('ACTIVE')).toBeInTheDocument();

    // Command 409 shows execution version conflict
    vi.mocked(api).mockRejectedValueOnce(new ApiError(409, 'version-conflict'));
    await userEvent.click(screen.getByRole('button', { name: 'Pause Execution' }));
    expect(await screen.findByText('Conflict: Execution version has changed concurrently.')).toBeInTheDocument();
  });

  test('shows epic version conflict on execution-start 409', async () => {
    vi.mocked(api).mockRejectedValueOnce(new ApiError(409, 'version-conflict'));
    render(<DeliveryWorkspace epicId={epicId} epicVersion={7} />);
    await userEvent.click(screen.getByRole('button', { name: 'Start Execution' }));
    expect(await screen.findByText('Conflict: The epic version changed on the server before starting execution.')).toBeInTheDocument();
  });
});
