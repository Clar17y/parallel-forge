import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { DeliveryWorkspace } from './delivery-workspace';
import { api } from '@/lib/api/client';
import { useEpicWorkspace } from '@/hooks/epics/use-epic-workspace';
import type { EpicExecutionDispatch, EpicExecutionProjection, GraphRevisionResponse } from '@/hooks/epics/types';

vi.mock('@/lib/api/client', async () => ({ ...await vi.importActual<typeof import('@/lib/api/client')>('@/lib/api/client'), api: vi.fn() }));
vi.mock('@/hooks/epics/use-epic-workspace', () => ({ useEpicWorkspace: vi.fn() }));

describe('DeliveryWorkspace readiness and sequential dispatch', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';
  const executionId = '22222222-2222-4222-8222-222222222222';
  const briefId = '33333333-3333-4333-8333-333333333333';
  const graphId = '44444444-4444-4444-8444-444444444444';
  const blockedItemId = '66666666-6666-4666-8666-666666666666';
  const runId = '88888888-8888-4888-8888-888888888888';
  const graph: GraphRevisionResponse = {
    schema_version: 1, epic_id: epicId, epic_version: 7, brief_revision_id: briefId, brief_digest: 'b'.repeat(64), revision_number: 2, created_at: '2026-10-01T00:00:00Z', graph_revision_id: graphId, graph_digest: 'g'.repeat(64),
    items: [{ item_id: blockedItemId, graph_revision_id: graphId, item_digest: 'i'.repeat(64), title: 'Integrated predecessor item', outcome: 'The predecessor is integrated', disposition: 'required', ordinal: 0, source_requirement_ids: [], dependency_item_ids: [], acceptance_criteria: ['Verified integration exists'] }],
  };
  let projection: EpicExecutionProjection;
  let dispatch: EpicExecutionDispatch | undefined;

  beforeEach(() => {
    resetEpicMutationStoreForTesting(); sessionStorage.clear(); vi.clearAllMocks(); dispatch = undefined;
    projection = {
      execution: { epic_id: epicId, execution_id: executionId, brief_revision_id: briefId, brief_digest: 'b'.repeat(64), graph_revision_id: graphId, graph_digest: 'g'.repeat(64), created_at: '2026-10-01T00:00:00Z' },
      control_version: 3, control_state: 'ACTIVE', blocker_code: null,
      children: [{
        attempt: { actor_id: 'actor', actual_epic_version: 7, attempt_id: 'attempt-1', attempt_number: 1, base_ref: 'main', base_sha: 'a'.repeat(40), blocker_codes: [], brief_digest: 'b'.repeat(64), brief_revision_id: briefId, context_digest: 'c'.repeat(64), created_at: '2026-10-01T00:00:00Z', dependency_evidence: [], epic_id: epicId, execution_id: executionId, expected_epic_version: 7, graph_digest: 'g'.repeat(64), graph_revision_id: graphId, item_digest: 'i'.repeat(64), item_disposition: 'required', item_id: blockedItemId, override_note: null, owner_override: false, run_id: runId, task_digest: 't'.repeat(64), task_id: 'task-1' },
        run_version: 5, run_state: 'COMPLETED', effects_settled: true, pending_gate: null, retained_gate: null,
      }], intents: [], owner_actions: [],
      items: [{ schema_version: 1, item_id: blockedItemId, disposition: 'required', status: 'blocked', blocker_code: 'predecessor_integration_unverified', dependency_evidence: [{ item_id: '99999999-9999-4999-8999-999999999999', status: 'unverified', blocker_code: 'predecessor_integration_unverified', predecessor_run_id: runId, integrated_sha: null, handoff_id: null }], completion_evidence: { item_id: blockedItemId, status: 'unverified', blocker_code: 'predecessor_integration_unverified', predecessor_run_id: runId, integrated_sha: null, handoff_id: null } }],
    };
    vi.mocked(useEpicWorkspace).mockReturnValue({ graphRevisions: [graph], acceptedGraph: undefined, loadingGraphRevisions: false } as unknown as ReturnType<typeof useEpicWorkspace>);
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/executions/${executionId}`) return { ...projection, ...(dispatch ? { dispatch } : {}) } as T;
      if (path === `/epics/${epicId}/executions` && !init?.method) return [{ ...projection, ...(dispatch ? { dispatch } : {}) }] as T;
      if (path === `/epics/${epicId}/executions/${executionId}/dispatch` && init?.method === 'PUT') {
        const body = JSON.parse(String(init.body));
        const headers = init.headers as Record<string, string>;
        expect(headers['Idempotency-Key']).toBeTruthy();
        dispatch = { schema_version: 1, execution_id: executionId, version: body.expected_dispatch_version + 1, enabled: body.enabled, profile_id: null, profile_version: null, blocker_code: null, enabled_by_actor_id: null, claim_item_id: null, claim_expires_at: null };
        return dispatch as T;
      }
      if (path.endsWith('/budget')) return { schema_version: 1, budget_policy_id: 'budget', budget_policy_version: 1, scope_type: 'epic', scope_id: epicId, version: 1, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z', task_budget: { max_duration_seconds: 10, max_cost_minor: 10, max_input_tokens: 10, max_output_tokens: 10, max_tool_calls: 10, max_provider_attempts: 1, disabled_dimensions: [] } } as T;
      return [] as T;
    });
  });
  afterEach(() => { cleanup(); sessionStorage.clear(); });

  test('uses server readiness when a child run completed without integration proof', async () => {
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    expect(await screen.findByText('Blocked: The predecessor is not verified as integrated.')).toBeInTheDocument();
    expect(screen.getByText('Child run status: COMPLETED')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'Open run' })[0]).toHaveAttribute('href', `/runs/${runId}`);
    expect(screen.queryByText('Verified')).not.toBeInTheDocument();
    await userEvent.click(screen.getByText('Inspect readiness evidence'));
    expect(screen.getAllByText(/handoff_id: null/)).toHaveLength(2);
  });

  test('enables then disables sequential delivery with expected dispatch versions and the same replay store', async () => {
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId={executionId} epicVersion={7} />);
    expect(await screen.findByText('Sequential delivery is off.')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Enable Sequential Delivery' }));
    expect(await screen.findByText('Sequential delivery is on.')).toBeInTheDocument();
    expect(vi.mocked(api).mock.calls.filter(([, init]) => init?.method === 'PUT').map(([, init]) => JSON.parse(String(init?.body)))).toEqual([
      { schema_version: 1, expected_dispatch_version: 0, enabled: true, profile_id: null, profile_version: null },
    ]);
    await userEvent.click(screen.getByRole('button', { name: 'Disable Sequential Delivery' }));
    expect(await screen.findByText('Sequential delivery is off.')).toBeInTheDocument();
    expect(vi.mocked(api).mock.calls.filter(([, init]) => init?.method === 'PUT').map(([, init]) => JSON.parse(String(init?.body)))).toEqual([
      { schema_version: 1, expected_dispatch_version: 0, enabled: true, profile_id: null, profile_version: null },
      { schema_version: 1, expected_dispatch_version: 1, enabled: false, profile_id: null, profile_version: null },
    ]);
  });
});
