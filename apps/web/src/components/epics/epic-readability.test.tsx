import { describe, expect, test, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describeAuthoringOutcome } from './authoring-presentation';
import { EpicList } from './epic-list';
import { groupActiveBlockers } from './delivery-workspace';
import type { AuthoringOutcome, EpicResponse } from '@/hooks/epics/types';
import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';

const mockPush = vi.fn();
const mockReplace = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush, replace: mockReplace }),
  usePathname: () => window.location.pathname,
  useSearchParams: () => new URL(window.location.href).searchParams,
}));

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) {
      super(code);
    }
  },
}));

describe('Epic Readability & Density Improvements', () => {
  beforeEach(() => {
    resetEpicMutationStoreForTesting();
    vi.clearAllMocks();
    sessionStorage.clear();
    window.history.replaceState({}, '', '/');
  });

  afterEach(() => {
    resetEpicMutationStoreForTesting();
    cleanup();
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  test('an adopted proposal reports "Proposal adopted" rather than "ready for review"', () => {
    const outcome: AuthoringOutcome = {
      schema_version: 1,
      job_id: 'job-adopted',
      job_version: 2,
      state: 'proposed',
      process_settled: true,
      usage_known: true,
      proposal_digest: 'p'.repeat(64),
      adopted_revision_id: 'rev-adopted-123',
    };

    const presentation = describeAuthoringOutcome(outcome);
    expect(presentation.title).toBe('Proposal adopted');
    expect(presentation.title).not.toMatch(/ready for review/i);
    expect(presentation.description).toMatch(/adopted/i);
  });

  test('EpicList uses stable structured layout and allows reading full problem statement', () => {
    const epics: EpicResponse[] = [
      {
        epic_id: 'epic-12345678-abcd',
        project_id: 'proj-1',
        title: 'Very Long Epic Title That Could Previously Squeeze Badges And Workspace Action In Narrow Containers',
        version: 1,
        schema_version: 1,
        created_at: '2026-10-01T00:00:00Z',
        updated_at: '2026-10-02T00:00:00Z',
        draft: {
          schema_version: 1,
          problem: 'This is a detailed and long problem description that explains why this epic exists and what requirements it satisfies in great depth.',
        },
        accepted_brief_revision_id: 'rev-1',
        accepted_brief_digest: 'b'.repeat(64),
        accepted_graph_revision_id: null,
        accepted_graph_digest: null,
      },
    ];

    render(<EpicList epics={epics} />);

    expect(screen.getByText('Brief Accepted')).toBeInTheDocument();
    expect(screen.getByText('No Graph')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Open Workspace/i })).toBeInTheDocument();
    // Full problem description should be discoverable/readable
    expect(screen.getAllByText(/This is a detailed and long problem description/i).length).toBeGreaterThan(0);
  });

  test('deduplicates blocker reasons in human terms with affected counts and targets without erasing origin', () => {
    const executionBlocker = 'predecessor_not_started';
    const children = [
      {
        attempt: {
          run_id: 'run-1',
          item_id: 'item-1',
          blocker_codes: ['predecessor_not_started'],
        },
      },
      {
        attempt: {
          run_id: 'run-2',
          item_id: 'item-2',
          blocker_codes: ['predecessor_not_started', 'active_child'],
        },
      },
    ];
    const itemTitles = new Map([
      ['item-1', 'Setup Database'],
      ['item-2', 'Run Migrations'],
    ]);

    const grouped = groupActiveBlockers(executionBlocker, children, itemTitles);
    expect(grouped).toHaveLength(2);

    const predBlocker = grouped.find((g: any) => g.code === 'predecessor_not_started');
    expect(predBlocker).toBeDefined();
    expect(predBlocker!.humanReason).toMatch(/prerequisite work item has not started/i);
    expect(predBlocker!.affectedCount).toBe(3); // execution + 2 children
    expect(predBlocker!.targets.map((t: any) => t.label)).toEqual([
      'Execution',
      'Setup Database',
      'Run Migrations',
    ]);

    const activeChildBlocker = grouped.find((g: any) => g.code === 'active_child');
    expect(activeChildBlocker).toBeDefined();
    expect(activeChildBlocker!.humanReason).toMatch(/another child run is still active/i);
    expect(activeChildBlocker!.affectedCount).toBe(1);
    expect(activeChildBlocker!.targets.map((t: any) => t.label)).toEqual(['Run Migrations']);
  });

  test('accepted brief cards expose complete requirement text at every length when expanded', async () => {
    const epicId = 'epic-brief-test-1';
    const testEpic = {
      epic_id: epicId,
      project_id: 'proj-1',
      title: 'Brief Readability Epic',
      version: 2,
      schema_version: 1,
      draft: { problem: 'Draft', requirements: [] },
      accepted_brief_revision_id: 'rev-acc-1',
      accepted_brief_digest: 'd'.repeat(64),
      accepted_graph_revision_id: null,
      accepted_graph_digest: null,
    };
    const testAcceptedBrief = {
      brief_revision_id: 'rev-acc-1',
      brief_digest: 'd'.repeat(64),
      problem: 'This is the core problem statement.',
      requirements: [
        {
          requirement_id: 'req-short',
          text: 'Keep every accepted requirement readable on small screens.',
          acceptance_criteria: ['Criterion 1'],
        },
        {
          requirement_id: 'req-boundary',
          text: 'Keep accepted requirements readable at every screen width.'.padEnd(80, '.'),
          acceptance_criteria: [],
        },
        {
          requirement_id: 'req-multiline',
          text: 'First line\nSecond line',
          acceptance_criteria: [],
        },
        {
          requirement_id: 'req-long',
          text: 'This is a very long requirement description that goes into extraordinary detail about multiple facets of the implementation and should therefore be summarized with a short preview rather than duplicated entirely across both summary and body.',
          acceptance_criteria: ['Criterion A', 'Criterion B'],
        },
      ],
      outcomes: ['Outcome 1'],
      scope: ['Scope item 1'],
      exclusions: [],
      decisions: [],
      assumptions: [],
      open_questions: [],
    };

    const { api } = await import('@/lib/api/client');
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return testEpic as T;
      if (path === `/epics/${epicId}/accepted-brief`) return testAcceptedBrief as T;
      if (path === `/epics/${epicId}/brief-revisions`) return [] as T;
      return [] as T;
    });

    const { BriefEditor } = await import('./brief-editor');
    render(<BriefEditor epicId={epicId} />);

    // Defaults to accepted subview when accepted_brief_revision_id exists
    expect(await screen.findByText('Accepted requirements baseline')).toBeInTheDocument();

    // Independent requirement disclosures closed by default
    const detailsCards = screen.getAllByRole('group');
    for (const card of detailsCards) {
      if (card.tagName.toLowerCase() === 'details') {
        expect(card).not.toHaveAttribute('open');
      }
    }

    // Empty decisions/assumptions/questions renders concise empty context without 3 blank headings
    expect(screen.queryByRole('heading', { name: 'Decisions' })).not.toBeInTheDocument();
    expect(screen.getByText(/no decisions, assumptions, or open questions/i)).toBeInTheDocument();

    // A one-line summary may clip at any character count on narrow screens.
    for (const requirement of testAcceptedBrief.requirements) {
      const fullText = screen.getAllByText(requirement.text, { exact: true, collapseWhitespace: false })
        .find(element => element.closest('.disclosure-body'));
      expect(fullText).toBeDefined();
      expect(fullText).not.toBeVisible();
      const card = fullText!.closest('details')!;
      await userEvent.click(card.querySelector('summary')!);
      expect(fullText).toBeVisible();
      await userEvent.click(card.querySelector('summary')!);
      expect(fullText).not.toBeVisible();
    }
  });

  test('accepted graph reading view provides item overview with human source requirement labels, criteria, and dependencies', async () => {
    const epicId = 'epic-graph-test-1';
    const testEpic = {
      epic_id: epicId,
      project_id: 'proj-1',
      title: 'Graph Readability Epic',
      version: 3,
      schema_version: 1,
      draft: { problem: 'Draft', requirements: [] },
      accepted_brief_revision_id: 'brief-1',
      accepted_brief_digest: 'b'.repeat(64),
      accepted_graph_revision_id: 'graph-1',
      accepted_graph_digest: 'g'.repeat(64),
    };
    const testBrief = {
      brief_revision_id: 'brief-1',
      brief_digest: 'b'.repeat(64),
      requirements: [
        { requirement_id: 'r-1', text: 'Full text of source requirement 1', acceptance_criteria: ['C1'] },
      ],
    };
    const testGraph = {
      graph_revision_id: 'graph-1',
      graph_digest: 'g'.repeat(64),
      brief_revision_id: 'brief-1',
      brief_digest: 'b'.repeat(64),
      items: [
        {
          item_id: 'i-1',
          title: 'Database Setup',
          outcome: 'Schema migrated',
          disposition: 'required' as const,
          ordinal: 0,
          source_requirement_ids: ['r-1'],
          dependency_item_ids: [],
          acceptance_criteria: ['Postgres tables ready'],
        },
      ],
      readiness: [
        { item_id: 'i-1', status: 'ready' as const, reason: 'All prerequisites satisfied', dependency_item_ids: [] },
      ],
    };

    const { api } = await import('@/lib/api/client');
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return testEpic as T;
      if (path === `/epics/${epicId}/accepted-graph`) return testGraph as T;
      if (path === `/epics/${epicId}/accepted-brief`) return testBrief as T;
      if (path === `/epics/${epicId}/graph-revisions`) return [testGraph] as T;
      return [] as T;
    });

    const { GraphEditor } = await import('./graph-editor');
    render(<GraphEditor epicId={epicId} />);

    // Defaults to accepted subview when accepted graph exists
    expect(await screen.findByText('Accepted work-item baseline')).toBeInTheDocument();
    // Human source requirement label and accessible full text
    expect(screen.getByText(/R1: Full text of source requirement 1/i)).toBeInTheDocument();
    // Item criteria displayed
    expect(screen.getByText('Postgres tables ready')).toBeInTheDocument();
    // Structural readiness badge distinct from delivery admission
    expect(screen.getByText('ready')).toBeInTheDocument();
  });

  test('incomplete brief adoption gives actionable missing-content explanation', async () => {
    const epicId = 'epic-adopt-test-1';
    const testEpic = {
      epic_id: epicId,
      project_id: 'proj-1',
      title: 'Incomplete Brief Epic',
      version: 2,
      schema_version: 1,
      draft: { problem: '', requirements: [] },
      accepted_brief_revision_id: null,
      accepted_brief_digest: null,
      accepted_graph_revision_id: null,
      accepted_graph_digest: null,
    };
    const incompleteRevision = {
      brief_revision_id: 'rev-incomplete',
      revision_number: 1,
      epic_id: epicId,
      epic_version: 2,
      content_digest: 'inc'.repeat(21) + 'i',
      created_at: '2026-10-01T00:00:00Z',
      content: {
        schema_version: 1,
        problem: '', // missing
        outcomes: [], // missing
        scope: [],
        exclusions: [],
        requirements: [
          { requirement_id: 'r1', text: 'Req without criteria', acceptance_criteria: [] }, // missing criteria
        ],
        decisions: [],
        assumptions: [],
        open_questions: [],
      },
    };

    const { api, ApiError } = await import('@/lib/api/client');
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}`) return testEpic as T;
      if (path === `/epics/${epicId}/brief-revisions`) return [incompleteRevision] as T;
      if (path === `/epics/${epicId}/brief-adoptions` && init?.method === 'POST') {
        throw new ApiError(422, 'request cannot be processed');
      }
      return [] as T;
    });

    const { userEvent } = await import('@testing-library/user-event');
    const { BriefEditor } = await import('./brief-editor');
    render(<BriefEditor epicId={epicId} />);

    // Go to history tab
    const historyBtn = await screen.findByRole('button', { name: /History & Revisions/i });
    await userEvent.click(historyBtn);

    const adoptBtn = screen.getByRole('button', { name: /Adopt revision #1/i });
    await userEvent.click(adoptBtn);

    // Expect actionable missing-content message, not only generic "Validation failed. Check the required fields."
    expect(await screen.findByRole('alert')).toHaveTextContent(/problem statement/i);
    expect(screen.getByRole('alert')).toHaveTextContent(/at least one outcome/i);
    expect(screen.getByRole('alert')).toHaveTextContent(/acceptance criteria for Requirement #1/i);
  });

  test('compact budget table displays scannable comparison with human units and without raw null', async () => {
    const epicId = 'epic-budget-test-1';
    const budgetData = {
      epic_id: epicId,
      initialized: true,
      version: 5,
      ceiling: {
        billing_mode: 'allowance_only',
        max_duration_seconds: 3600,
        max_cost_minor: 500,
        max_input_tokens: '9223372036854775807',
        max_output_tokens: null,
        max_tool_calls: 50,
        max_provider_attempts: 3,
        max_named_checks: 10,
        max_repairs: 3,
      },
      disabled_dimensions: ['provider_attempts'],
      known: {
        duration_ms: 0,
        tool_call_count: 0,
        input_tokens: 4500,
        output_tokens: 800,
        estimated_api_cost_minor: 0,
        provider_attempts: 1,
      },
      held: {
        duration_ms: 0,
        tool_call_count: 0,
        input_tokens: 0,
        output_tokens: 0,
        estimated_api_cost_minor: 0,
        provider_attempts: 0,
      },
      unknown: false,
      currency: 'USD',
      warnings: [],
      permits: [],
      owner_actions: [],
    };

    const { api } = await import('@/lib/api/client');
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}/budget`) return budgetData as T;
      return undefined as T;
    });

    const { EpicBudgetPanel } = await import('./epic-budget-panel');
    render(<EpicBudgetPanel epicId={epicId} />);

    // Table view rendered
    expect(await screen.findByRole('table')).toBeInTheDocument();

    // Human duration ceiling: 3600s is formatted as 1h, not duplicate 3600s / 0ms (0ms)
    const durationCeiling = screen.getByTestId('ceiling-duration_ms');
    expect(durationCeiling).toHaveTextContent('1h');
    expect(durationCeiling).not.toHaveTextContent('3600s');

    // Known duration: 0ms formatted as 0ms, not "0ms (0ms)"
    const durationKnown = screen.getByTestId('known-duration_ms');
    expect(durationKnown).toHaveTextContent('0ms');
    expect(durationKnown.textContent).not.toMatch(/0ms\s*\(0ms\)/);

    // No raw null in normal UI
    expect(screen.queryByText(/null/i)).not.toBeInTheDocument();
    expect(screen.getByTestId('ceiling-output_tokens')).toHaveTextContent(/Unlimited/i);

    // Large 64-bit integer strings formatted without loss of precision
    expect(screen.getByTestId('ceiling-input_tokens')).toHaveTextContent('9,223,372,036,854,775,807');
  });

  test('delivery workspace removes redundant paragraphs and keeps owner warnings visible', async () => {
    const epicId = 'epic-delivery-test-1';
    const testExecution = {
      execution: {
        epic_id: epicId,
        execution_id: 'exec-1',
        brief_revision_id: 'b-1',
        brief_digest: 'b'.repeat(64),
        graph_revision_id: 'g-1',
        graph_digest: 'g'.repeat(64),
        created_at: '2026-10-01T00:00:00Z',
      },
      control_version: 1,
      control_state: 'ACTIVE',
      blocker_code: null,
      children: [],
      intents: [],
      owner_actions: [
        {
          actor_id: 'act-1',
          event_type: 'override_launch',
          version: 1,
          warnings: ['Critical owner warning: manual override applied.'],
        },
      ],
      items: [],
      dispatch: {
        version: 1,
        enabled: false,
        profile_id: null,
        profile_version: null,
        enabled_by_actor_id: null,
        claim_item_id: null,
        claim_expires_at: null,
        blocker_code: null,
      },
    };

    const { api } = await import('@/lib/api/client');
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}/executions/exec-1`) return testExecution as T;
      if (path === `/epics/${epicId}/executions`) return [testExecution] as T;
      if (path === `/epics/${epicId}`) return { epic_id: epicId, version: 1 } as T;
      return undefined as T;
    });

    const { DeliveryWorkspace } = await import('./delivery-workspace');
    render(<DeliveryWorkspace epicId={epicId} initialExecutionId="exec-1" epicVersion={1} />);

    // Sequential delivery does not have 3 redundant paragraphs
    expect(await screen.findByText(/Sequential delivery is off\./i)).toBeInTheDocument();
    expect(screen.queryByText(/The server currently reports automatic admission disabled/i)).not.toBeInTheDocument();

    // Owner warning remains directly visible, not collapsed inside disclosure
    expect(screen.getByText('Critical owner warning: manual override applied.')).toBeVisible();
  });

  test('stage-aware initial visit chooses delivery for accepted graph, brief for accepted brief, and authoring for new epic', async () => {
    const { api } = await import('@/lib/api/client');
    const EpicPage = (await import('@/app/epics/[epicId]/page')).default;

    // Case 1: Epic with accepted graph -> defaults to Delivery
    const epicWithGraph = {
      epic_id: 'epic-stage-graph',
      project_id: 'proj-1',
      title: 'Graph Accepted Epic',
      version: 3,
      schema_version: 1,
      draft: { problem: 'P', requirements: [] },
      accepted_brief_revision_id: 'b-1',
      accepted_brief_digest: 'b'.repeat(64),
      accepted_graph_revision_id: 'g-1',
      accepted_graph_digest: 'g'.repeat(64),
    };

    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === '/epics/epic-stage-graph') return epicWithGraph as T;
      if (path.startsWith('/epics/epic-stage-graph/executions')) return [] as T;
      return [] as T;
    });

    window.history.replaceState({}, '', '/epics/epic-stage-graph');
    let res: any;
    await act(async () => {
      res = render(<EpicPage params={Promise.resolve({ epicId: 'epic-stage-graph' })} />);
    });

    expect(await screen.findByRole('button', { name: 'Delivery & Progress' })).toHaveAttribute('aria-current', 'page');
    res.unmount();

    // Case 2: Epic with accepted brief only -> defaults to Manual Requirements Brief
    const epicWithBrief = {
      epic_id: 'epic-stage-brief',
      project_id: 'proj-1',
      title: 'Brief Accepted Epic',
      version: 2,
      schema_version: 1,
      draft: { problem: 'P', requirements: [] },
      accepted_brief_revision_id: 'b-1',
      accepted_brief_digest: 'b'.repeat(64),
      accepted_graph_revision_id: null,
      accepted_graph_digest: null,
    };

    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === '/epics/epic-stage-brief') return epicWithBrief as T;
      if (path.startsWith('/epics/epic-stage-brief/accepted-brief')) return { ...epicWithBrief.draft, brief_revision_id: 'b-1', brief_digest: 'b'.repeat(64) } as T;
      return [] as T;
    });

    window.history.replaceState({}, '', '/epics/epic-stage-brief');
    let res2: any;
    await act(async () => {
      res2 = render(<EpicPage params={Promise.resolve({ epicId: 'epic-stage-brief' })} />);
    });

    expect(await screen.findByRole('button', { name: 'Manual Requirements Brief' })).toHaveAttribute('aria-current', 'page');
    res2.unmount();

    // Case 3: Explicit tab query ?tab=authoring wins over accepted graph stage default
    window.history.replaceState({}, '', '/epics/epic-stage-graph?tab=authoring');
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === '/epics/epic-stage-graph') return epicWithGraph as T;
      return [] as T;
    });

    await act(async () => {
      render(<EpicPage params={Promise.resolve({ epicId: 'epic-stage-graph' })} />);
    });
    expect(await screen.findByRole('button', { name: 'Brainstorm' })).toHaveAttribute('aria-current', 'page');
  });
});
