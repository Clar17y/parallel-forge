import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { DeliveryWorkspace } from './delivery-workspace';
import { ApiError, api } from '@/lib/api/client';
import { useEpicWorkspace } from '@/hooks/epics/use-epic-workspace';
import type { EpicExecutionProjection, GraphRevisionResponse } from '@/hooks/epics/types';

vi.mock('@/lib/api/client', async () => ({ ...await vi.importActual<typeof import('@/lib/api/client')>('@/lib/api/client'), api: vi.fn() }));
vi.mock('@/hooks/epics/use-epic-workspace', () => ({ useEpicWorkspace: vi.fn() }));

describe('DeliveryWorkspace manual child launch', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';
  const executionId = '22222222-2222-4222-8222-222222222222';
  const briefId = '33333333-3333-4333-8333-333333333333';
  const graphId = '44444444-4444-4444-8444-444444444444';
  const itemId = '66666666-6666-4666-8666-666666666666';
  const runId = '88888888-8888-4888-8888-888888888888';
  const projection: EpicExecutionProjection = {
    execution: { epic_id: epicId, execution_id: executionId, brief_revision_id: briefId, brief_digest: 'b'.repeat(64), graph_revision_id: graphId, graph_digest: 'g'.repeat(64), created_at: '2026-10-01T00:00:00Z' },
    control_version: 3, control_state: 'ACTIVE', blocker_code: null, children: [], intents: [], owner_actions: [], items: [],
  };
  const graph: GraphRevisionResponse = {
    schema_version: 1, epic_id: epicId, epic_version: 7, brief_revision_id: briefId, brief_digest: 'b'.repeat(64), revision_number: 2, created_at: '2026-10-01T00:00:00Z', graph_revision_id: graphId, graph_digest: 'g'.repeat(64),
    items: [{ item_id: itemId, graph_revision_id: graphId, item_digest: 'i'.repeat(64), title: 'Saved deferred item', outcome: 'Keep its disposition', disposition: 'deferred', ordinal: 0, source_requirement_ids: [], dependency_item_ids: [], acceptance_criteria: ['Keep the record'] }],
  };

  beforeEach(() => {
    resetEpicMutationStoreForTesting(); sessionStorage.clear(); vi.clearAllMocks();
    vi.mocked(useEpicWorkspace).mockReturnValue({ graphRevisions: [graph], acceptedGraph: undefined, loadingGraphRevisions: false } as unknown as ReturnType<typeof useEpicWorkspace>);
    vi.mocked(api).mockImplementation(async <T,>(path: string) => path.endsWith('/executions') ? [projection] as T : path.endsWith('/budget') ? undefined as T : [] as T);
  });
  afterEach(() => { cleanup(); sessionStorage.clear(); });

  test('launches the selected saved item against the frozen execution and links the returned run', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}`) return projection as T;
      if (path === `/epics/${epicId}/work-item-runs` && init?.method === 'POST') {
        const body = JSON.parse(String(init.body));
        expect(body).toEqual({ schema_version: 1, expected_epic_version: 7, execution_id: executionId, brief_revision_id: briefId, brief_digest: 'b'.repeat(64), graph_revision_id: graphId, graph_digest: 'g'.repeat(64), item_id: itemId, owner_override: false });
        expect(init.headers).toHaveProperty('Idempotency-Key');
        return { epic_id: epicId, execution_id: executionId, item_id: itemId, item_disposition: 'deferred', run_id: runId, attempt_id: 'attempt-1', attempt_number: 1, blocker_codes: ['item_deferred'] } as T;
      }
      if (path.endsWith('/budget')) return undefined as T;
      return [] as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    await userEvent.selectOptions(await screen.findByLabelText('Saved work item'), itemId);
    await userEvent.click(await screen.findByRole('button', { name: 'Launch Work Item' }));
    expect(await screen.findByRole('link', { name: /Open launched run/i })).toHaveAttribute('href', `/runs/${runId}`);
    expect(screen.getByText(/saved disposition: deferred/i)).toBeInTheDocument();
  });

  test('shows the server blocker and makes owner override a new explicit request', async () => {
    let postCount = 0;
    let firstKey = '';
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}`) return projection as T;
      if (path === `/epics/${epicId}/work-item-runs` && init?.method === 'POST') {
        postCount += 1;
        const headers = init.headers as Record<string, string>;
        if (postCount === 1) {
          firstKey = headers['Idempotency-Key'];
          throw new ApiError(409, 'epic_launch_blocked', {}, { code: 'epic_launch_blocked', blocker_codes: ['item_deferred'], actual_epic_version: 7, owner_action: 'retry_with_owner_override' });
        }
        expect(headers['Idempotency-Key']).not.toBe(firstKey);
        const body = JSON.parse(String(init.body));
        expect(body.owner_override).toBe(true);
        expect(body.override_note).toBe('Owner proceeding despite the warning');
        return { epic_id: epicId, execution_id: executionId, item_id: itemId, item_disposition: 'deferred', run_id: runId, attempt_id: 'attempt-2', attempt_number: 1, blocker_codes: ['item_deferred'] } as T;
      }
      return path.endsWith('/budget') ? undefined as T : [] as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    await userEvent.selectOptions(await screen.findByLabelText('Saved work item'), itemId);
    await userEvent.click(screen.getByRole('button', { name: 'Launch Work Item' }));
    expect(await screen.findByText('This work item is saved as deferred.')).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText(/Owner override: launch despite server-reported workflow warnings/i));
    await userEvent.type(screen.getByLabelText('Override note (optional)'), 'Owner proceeding despite the warning');
    await userEvent.click(screen.getByRole('button', { name: 'Launch Work Item' }));
    expect(await screen.findByRole('link', { name: /Open launched run/i })).toHaveAttribute('href', `/runs/${runId}`);
    expect(postCount).toBe(2);
  });

  test('uncertain launch retries the exact original body and idempotency key', async () => {
    let firstRequest: { body: string; key: string } | undefined;
    let postCount = 0;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}`) return projection as T;
      if (path === `/epics/${epicId}/work-item-runs` && init?.method === 'POST') {
        postCount += 1;
        const request = { body: String(init.body), key: (init.headers as Record<string, string>)['Idempotency-Key'] };
        if (!firstRequest) { firstRequest = request; throw new ApiError(503, 'request-failed'); }
        expect(request).toEqual(firstRequest);
        return { epic_id: epicId, execution_id: executionId, item_id: itemId, item_disposition: 'deferred', run_id: runId, attempt_id: 'attempt-replayed', attempt_number: 1, blocker_codes: [] } as T;
      }
      return path.endsWith('/budget') ? undefined as T : [] as T;
    });
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    await userEvent.selectOptions(await screen.findByLabelText('Saved work item'), itemId);
    await userEvent.click(screen.getByRole('button', { name: 'Launch Work Item' }));
    const retry = await screen.findByRole('button', { name: 'Retry original request' });
    await userEvent.click(retry);
    expect(await screen.findByRole('link', { name: /Open launched run/i })).toHaveAttribute('href', `/runs/${runId}`);
    expect(postCount).toBe(2);
  });

  test.each([
    ['selection', true], ['selection', false],
    ['route', true], ['route', false],
    ['manual load', true], ['manual load', false],
  ] as const)('resets launch state after %s changes execution (preserved item: %s)', async (change, preservedItem) => {
    const nextExecutionId = '99999999-9999-4999-8999-999999999999';
    const nextGraphId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
    const nextItemId = preservedItem ? itemId : 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
    const nextProjection: EpicExecutionProjection = {
      ...projection,
      execution: { ...projection.execution, execution_id: nextExecutionId, graph_revision_id: nextGraphId, graph_digest: 'n'.repeat(64) },
    };
    const nextGraph: GraphRevisionResponse = {
      ...graph, graph_revision_id: nextGraphId, graph_digest: 'n'.repeat(64), revision_number: 3,
      items: graph.items.map(item => ({ ...item, item_id: nextItemId, graph_revision_id: nextGraphId })),
    };
    vi.mocked(useEpicWorkspace).mockReturnValue({ graphRevisions: [graph, nextGraph], acceptedGraph: undefined, loadingGraphRevisions: false } as unknown as ReturnType<typeof useEpicWorkspace>);
    const launchBodies: Record<string, unknown>[] = [];
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.endsWith('/executions')) return [projection, nextProjection] as T;
      if (path === `/epics/${epicId}/executions/${executionId}`) return projection as T;
      if (path === `/epics/${epicId}/executions/${nextExecutionId}`) return nextProjection as T;
      if (path === `/epics/${epicId}/work-item-runs` && init?.method === 'POST') {
        const body = JSON.parse(String(init.body));
        launchBodies.push(body);
        return { epic_id: epicId, execution_id: body.execution_id, item_id: body.item_id, item_disposition: 'deferred', run_id: runId, attempt_id: `attempt-${launchBodies.length}`, attempt_number: 1, blocker_codes: [] } as T;
      }
      return path.endsWith('/budget') ? undefined as T : [] as T;
    });
    const { rerender } = render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    await userEvent.selectOptions(await screen.findByLabelText('Saved work item'), itemId);
    await userEvent.click(screen.getByLabelText(/Owner override: launch despite server-reported workflow warnings/i));
    await userEvent.type(screen.getByLabelText('Override note (optional)'), 'First execution authority');
    await userEvent.click(screen.getByRole('button', { name: 'Launch Work Item' }));
    await screen.findByRole('link', { name: /Open launched run/i });
    // A new draft after the successful launch must also remain execution-local.
    await userEvent.type(screen.getByLabelText('Override note (optional)'), 'First execution draft');

    if (change === 'route') {
      rerender(<DeliveryWorkspace epicId={epicId} initialExecutionId={nextExecutionId} epicVersion={7} />);
    } else if (change === 'manual load') {
      await userEvent.click(screen.getByRole('button', { name: 'Start New Execution' }));
      await userEvent.type(screen.getByLabelText('Execution ID'), nextExecutionId);
      await userEvent.click(screen.getByRole('button', { name: 'Load Execution' }));
    } else {
      await userEvent.selectOptions(screen.getByLabelText('Select Execution'), nextExecutionId);
    }
    await waitFor(() => expect(screen.getByText(`graph_revision_id: ${nextGraphId}`)).toBeInTheDocument());
    expect(screen.getByLabelText('Saved work item')).toHaveValue('');
    expect(screen.getByLabelText(/Owner override: launch despite server-reported workflow warnings/i)).not.toBeChecked();
    expect(screen.queryByRole('link', { name: /Open launched run/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Launch Work Item' })).toBeDisabled();
    await userEvent.click(screen.getByLabelText(/Owner override: launch despite server-reported workflow warnings/i));
    expect(screen.getByLabelText('Override note (optional)')).toHaveValue('');
    await userEvent.click(screen.getByLabelText(/Owner override: launch despite server-reported workflow warnings/i));
    await userEvent.selectOptions(screen.getByLabelText('Saved work item'), nextItemId);
    await userEvent.click(screen.getByRole('button', { name: 'Launch Work Item' }));
    await waitFor(() => expect(launchBodies).toHaveLength(2));
    expect(launchBodies[0]).toMatchObject({ execution_id: executionId, owner_override: true, override_note: 'First execution authority' });
    expect(launchBodies[1]).toMatchObject({ execution_id: nextExecutionId, graph_revision_id: nextGraphId, graph_digest: 'n'.repeat(64), item_id: nextItemId, owner_override: false });
    expect(launchBodies[1]).not.toHaveProperty('override_note');
  });
});
