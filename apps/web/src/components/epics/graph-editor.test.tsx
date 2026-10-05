import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { GraphEditor } from './graph-editor';
import { api } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) {
      super(code);
    }
  },
}));

describe('GraphEditor', () => {
  const epicId = '22222222-3333-4444-5555-666666666666';
  const initialEpic = {
    epic_id: epicId,
    project_id: 'proj-1',
    title: 'Work Graph Epic',
    version: 4,
    schema_version: 1,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    draft: {
      problem: 'Need graph',
      requirements: [
        { requirement_id: 'req-1', text: 'Requirement 1', acceptance_criteria: ['Crit 1'] },
        { requirement_id: 'req-2', text: 'Requirement 2', acceptance_criteria: ['Crit 2'] },
      ],
    },
    accepted_brief_revision_id: 'brief-rev-1',
    accepted_brief_digest: 'b'.repeat(64),
    accepted_graph_revision_id: 'graph-rev-1',
    accepted_graph_digest: 'g'.repeat(64),
  };

  const initialGraphRevisions = [
    {
      schema_version: 1,
      graph_revision_id: 'graph-rev-1',
      revision_number: 1,
      epic_id: epicId,
      epic_version: 3,
      brief_revision_id: 'brief-rev-1',
      brief_digest: 'b'.repeat(64),
      graph_digest: 'g'.repeat(64),
      items: [
        {
          item_id: 'item-1',
          title: 'Database Schema Setup',
          outcome: 'Postgres tables created',
          disposition: 'required' as const,
          ordinal: 0,
          source_requirement_ids: ['req-1'],
          dependency_item_ids: [],
          acceptance_criteria: ['Tables exist'],
          graph_revision_id: 'graph-rev-1',
          item_digest: 'i1'.repeat(32),
        },
        {
          item_id: 'item-2',
          title: 'API Endpoints',
          outcome: 'REST routes available',
          disposition: 'required' as const,
          ordinal: 1,
          source_requirement_ids: ['req-1', 'req-2'],
          dependency_item_ids: ['item-1'],
          acceptance_criteria: ['Routes return 200'],
          graph_revision_id: 'graph-rev-1',
          item_digest: 'i2'.repeat(32),
        },
      ],
      readiness: [
        { item_id: 'item-1', status: 'ready' as const, reason: 'All dependencies satisfied', dependency_item_ids: [] },
        { item_id: 'item-2', status: 'blocked' as const, reason: 'Unfinished dependency item-1', dependency_item_ids: ['item-1'] },
      ],
      created_at: '2026-10-01T02:00:00Z',
    },
  ];

  beforeEach(() => { resetEpicMutationStoreForTesting();
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  afterEach(() => { resetEpicMutationStoreForTesting();
    cleanup();
    vi.mocked(api).mockReset();
    sessionStorage.clear();
  });

  test('displays items with title, disposition, dependencies, and readiness status without execution eligibility claim', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return initialEpic as T;
      if (path === `/epics/${epicId}/graph-revisions`) return initialGraphRevisions as T;
      if (path === `/epics/${epicId}/accepted-graph`) return initialGraphRevisions[0] as T;
      if (path === `/epics/${epicId}/accepted-brief`) return {
        epic_id: epicId,
        brief_revision_id: 'brief-rev-1',
        brief_digest: 'b'.repeat(64),
        requirements: initialEpic.draft.requirements,
      } as T;
      return [] as T;
    });

    render(<GraphEditor epicId={epicId} />);

    expect(await screen.findByDisplayValue('Database Schema Setup')).toBeInTheDocument();
    expect(screen.getByDisplayValue('API Endpoints')).toBeInTheDocument();

    // Check readiness projections displayed
    expect(screen.getByText('All dependencies satisfied')).toBeInTheDocument();
    expect(screen.getByText('Unfinished dependency item-1')).toBeInTheDocument();

    // Discloses that structural readiness does not equal execution eligibility
    expect(
      screen.getByText(/Structural readiness is verified by graph constraints; execution admission is managed separately/i)
    ).toBeInTheDocument();
  });

  test('allows adding new item, binding source requirement, and saving new graph revision', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return initialEpic as T;
      if (path === `/epics/${epicId}/graph-revisions`) return initialGraphRevisions as T;
      if (path === `/epics/${epicId}/accepted-graph`) return initialGraphRevisions[0] as T;
      if (path === `/epics/${epicId}/accepted-brief`) return {
        epic_id: epicId,
        brief_revision_id: 'brief-rev-1',
        brief_digest: 'b'.repeat(64),
        requirements: initialEpic.draft.requirements,
      } as T;
      return [] as T;
    });

    render(<GraphEditor epicId={epicId} />);
    await screen.findByDisplayValue('Database Schema Setup');

    // Add item
    const addBtn = screen.getByRole('button', { name: /\+ Add Item/i });
    await userEvent.click(addBtn);

    // Type title of new item (which will be the 3rd item)
    const titleInputs = screen.getAllByLabelText(/Item title/i);
    const lastTitleInput = titleInputs[titleInputs.length - 1];
    await userEvent.type(lastTitleInput, 'Frontend Dashboard');
    await userEvent.type(screen.getByLabelText('Item outcome 3'), 'Operators can view progress');
    await userEvent.type(screen.getByLabelText('Criterion 1 for item 3'), 'Saved progress can be reopened');
    await userEvent.click(screen.getAllByRole('button', { name: /Requirement 2/ }).at(-1)!);

    // Mock graph revision save
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/graph-revisions` && init?.method === 'POST') {
        return {
          schema_version: 1,
          graph_revision_id: 'graph-rev-2',
          revision_number: 2,
          epic_id: epicId,
          epic_version: 5,
          brief_revision_id: 'brief-rev-1',
          brief_digest: 'b'.repeat(64),
          graph_digest: 'g2'.repeat(32),
          items: [],
          created_at: '2026-10-01T03:00:00Z',
        } as T;
      }
      return [] as T;
    });

    const saveRevBtn = screen.getByRole('button', { name: /Save new graph revision/i });
    await userEvent.click(saveRevBtn);

    expect(api).toHaveBeenCalledWith(
      `/epics/${epicId}/graph-revisions`,
      expect.objectContaining({
        method: 'POST',
        body: expect.stringContaining('Frontend Dashboard'),
      })
    );
  });

  function serveGraph(revisions = initialGraphRevisions, getEpic = () => initialEpic) {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}`) return getEpic() as T;
      if (path.endsWith('/graph-revisions')) {
        if (init?.method === 'POST') return { ...revisions.at(-1), graph_revision_id: 'graph-rev-3', epic_version: 5, revision_number: 3, items: JSON.parse(init.body as string).items } as T;
        return revisions as T;
      }
      if (path.endsWith('/accepted-graph')) return initialGraphRevisions[0] as T;
      if (path.endsWith('/graph-adoptions') && init?.method === 'POST') return { ...getEpic(), version: 6, accepted_graph_revision_id: 'graph-rev-3' } as T;
      if (path.endsWith('/accepted-brief')) return { brief_revision_id: 'brief-rev-1', brief_digest: 'b'.repeat(64), requirements: initialEpic.draft.requirements } as T;
      return [] as T;
    });
  }

  test('reopens the latest saved matching graph even when an older revision is accepted', async () => {
    const latest = { ...initialGraphRevisions[0], graph_revision_id: 'graph-rev-2', revision_number: 2, items: [{ ...initialGraphRevisions[0].items[0], title: 'Latest saved work item' }] };
    serveGraph([...initialGraphRevisions, latest]);
    render(<GraphEditor epicId={epicId} />);
    expect(await screen.findByDisplayValue('Latest saved work item')).toBeInTheDocument();
    expect(screen.queryByDisplayValue('Database Schema Setup')).not.toBeInTheDocument();
  });

  test('removing an item also removes its references from dependent items', async () => {
    serveGraph();
    render(<GraphEditor epicId={epicId} />);
    await screen.findByDisplayValue('Database Schema Setup');
    await userEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0]);
    await userEvent.click(screen.getByRole('button', { name: 'Save new graph revision' }));
    const posted = vi.mocked(api).mock.calls.find(([path, init]) => path.endsWith('/graph-revisions') && init?.method === 'POST');
    expect(posted).toBeDefined();
    expect(JSON.parse(posted![1]!.body as string).items).toEqual([expect.objectContaining({ item_id: 'item-2', ordinal: 0, dependency_item_ids: [] })]);
  });

  test('an incomplete new item cannot be sent until its title, outcome, criteria and source are complete', async () => {
    serveGraph();
    render(<GraphEditor epicId={epicId} />);
    await screen.findByDisplayValue('Database Schema Setup');
    await userEvent.click(screen.getByRole('button', { name: '+ Add Item' }));
    expect(screen.getByRole('button', { name: 'Save new graph revision' })).toBeDisabled();
    await userEvent.type(screen.getByLabelText('Item title 3'), 'New item');
    await userEvent.type(screen.getByLabelText('Item outcome 3'), 'Bounded outcome');
    await userEvent.type(screen.getByLabelText('Criterion 1 for item 3'), 'Verifiable result');
    await userEvent.click(screen.getAllByRole('button', { name: /Requirement 2/ }).at(-1)!);
    expect(screen.getByRole('button', { name: 'Save new graph revision' })).toBeEnabled();
  });

  test('preserves dirty graph content after another tab changes the epic and requires an explicit comparison choice', async () => {
    let server = initialEpic;
    serveGraph(initialGraphRevisions, () => server);
    render(<GraphEditor epicId={epicId} />);
    const title = await screen.findByDisplayValue('Database Schema Setup');
    await userEvent.clear(title);
    await userEvent.type(title, 'My unsaved graph');
    server = { ...initialEpic, version: 5 };
    await act(async () => { window.dispatchEvent(new StorageEvent('storage', { key: `epic_refresh_${epicId}`, newValue: 'other-tab:5' })); });
    expect(screen.getByDisplayValue('My unsaved graph')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save new graph revision' })).toBeDisabled();
    expect(screen.getByText('Inspect current server graph')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Use my edited graph against this version' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save new graph revision' }));
    const posted = vi.mocked(api).mock.calls.find(([path, init]) => path.endsWith('/graph-revisions') && init?.method === 'POST');
    expect(JSON.parse(posted![1]!.body as string)).toMatchObject({ expected_epic_version: 5, brief_revision_id: 'brief-rev-1', items: [expect.objectContaining({ title: 'My unsaved graph' }), expect.any(Object)] });
  });

  test('a typed graph proposal remains distinct until copied, edited and saved as a normal revision', async () => {
    serveGraph();
    render(<GraphEditor epicId={epicId} proposedGraph={{ schema_version: 1, brief_revision_id: 'brief-rev-1', brief_digest: 'b'.repeat(64), proposal_digest: 'c'.repeat(64), items: [{ ...initialGraphRevisions[0].items[0], title: 'Proposed bounded item' }] }} />);
    expect(await screen.findByText('Graph proposal')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Database Schema Setup')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Edit proposed items' }));
    expect(screen.getByDisplayValue('Proposed bounded item')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Save new graph revision' }));
    const posted = vi.mocked(api).mock.calls.find(([path, init]) => path.endsWith('/graph-revisions') && init?.method === 'POST');
    expect(JSON.parse(posted![1]!.body as string).items).toEqual([expect.objectContaining({ title: 'Proposed bounded item' })]);
    expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/graph-adoptions'))).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: /Graph Revisions/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Adopt revision #3' }));
    const adopted = vi.mocked(api).mock.calls.find(([path]) => path.endsWith('/graph-adoptions'));
    expect(JSON.parse(adopted![1]!.body as string)).toMatchObject({ expected_epic_version: 5, graph_revision_id: 'graph-rev-3', graph_digest: 'g'.repeat(64) });
  });

  test('a failed accepted-brief fetch is distinct from an accepted brief with no requirements', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return initialEpic as T;
      if (path.endsWith('/accepted-brief')) throw new Error('temporarily unavailable');
      return [] as T;
    });
    render(<GraphEditor epicId={epicId} />);
    await screen.findByText('Work-Item Decomposition');
    await userEvent.click(screen.getByRole('button', { name: '+ Add Item' }));
    expect(screen.getByText('The accepted brief could not be loaded. Refresh the workspace to retry.')).toBeInTheDocument();
    expect(screen.queryByText('No requirements in accepted brief.')).not.toBeInTheDocument();
    expect(screen.queryByText('No Accepted Brief Baseline')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry accepted brief' })).toBeEnabled();
  });

  test('accepted graph evidence exposes immutable criteria and dependencies', async () => {
    serveGraph();
    render(<GraphEditor epicId={epicId} />);
    await screen.findByDisplayValue('Database Schema Setup');
    await userEvent.click(screen.getByRole('button', { name: 'Accepted Graph' }));
    await userEvent.click(screen.getByText('Inspect digests'));
    const snapshot = await screen.findByText(/Tables exist/);
    expect(snapshot).toBeVisible();
    expect(snapshot).toHaveTextContent('dependency_item_ids');
    expect(snapshot).toHaveTextContent('item-1');
  });

  test('failed graph history loading is distinct from having no saved revisions', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return initialEpic as T;
      if (path.endsWith('/graph-revisions')) throw new Error('temporary read failure');
      return [] as T;
    });
    render(<GraphEditor epicId={epicId} />);
    await screen.findByText('Work-Item Decomposition');
    await userEvent.click(screen.getByRole('button', { name: /Graph Revisions/ }));
    expect(screen.getByText('Graph history could not be loaded.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry graph history' })).toBeEnabled();
    expect(screen.queryByText('No graph revisions have been saved yet.')).not.toBeInTheDocument();
  });

  test('dirty graph combined with accepted-brief change preserves local items and blocks write until explicit rebase uses new binding', async () => {
    let server = initialEpic;
    let acceptedBrief = {
      epic_id: epicId,
      brief_revision_id: 'brief-rev-1',
      brief_digest: 'b'.repeat(64),
      requirements: initialEpic.draft.requirements,
    };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}`) return server as T;
      if (path === `/epics/${epicId}/graph-revisions` && init?.method === 'POST') {
        const body = JSON.parse(init.body as string);
        return {
          schema_version: 1,
          graph_revision_id: 'new-graph-rev',
          revision_number: 2,
          epic_id: epicId,
          epic_version: body.expected_epic_version + 1,
          brief_revision_id: body.brief_revision_id,
          brief_digest: body.brief_digest,
          graph_digest: 'new-digest'.padEnd(64, '0'),
          items: body.items,
          created_at: '2026-10-01T04:00:00Z',
        } as T;
      }
      if (path === `/epics/${epicId}/graph-revisions`) return initialGraphRevisions as T;
      if (path === `/epics/${epicId}/accepted-graph`) return initialGraphRevisions[0] as T;
      if (path === `/epics/${epicId}/accepted-brief`) return acceptedBrief as T;
      return [] as T;
    });

    render(<GraphEditor epicId={epicId} />);
    const titleInput = await screen.findByDisplayValue('Database Schema Setup');
    await userEvent.clear(titleInput);
    await userEvent.type(titleInput, 'Locally modified setup');

    const newBriefId = 'brief-rev-2-accepted';
    const newBriefDigest = 'c'.repeat(64);
    server = {
      ...initialEpic,
      version: 5,
      accepted_brief_revision_id: newBriefId,
      accepted_brief_digest: newBriefDigest,
    };
    acceptedBrief = {
      epic_id: epicId,
      brief_revision_id: newBriefId,
      brief_digest: newBriefDigest,
      requirements: initialEpic.draft.requirements,
    };

    await act(async () => {
      window.dispatchEvent(new StorageEvent('storage', { key: `epic_refresh_${epicId}`, newValue: 'refresh:5' }));
    });

    expect(screen.getByDisplayValue('Locally modified setup')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save new graph revision' })).toBeDisabled();
    expect(screen.getByText('The epic version changed on the server. Compare the latest graph below before replacing it.')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Use my edited graph against this version' }));
    expect(screen.getByRole('button', { name: 'Save new graph revision' })).toBeEnabled();

    await userEvent.click(screen.getByRole('button', { name: 'Save new graph revision' }));
    const posted = vi.mocked(api).mock.calls.find(([path, init]) => path.endsWith('/graph-revisions') && init?.method === 'POST');
    expect(posted).toBeDefined();
    const payload = JSON.parse(posted![1]!.body as string);
    expect(payload.expected_epic_version).toBe(5);
    expect(payload.brief_revision_id).toBe(newBriefId);
    expect(payload.brief_digest).toBe(newBriefDigest);
    expect(payload.items[0].title).toBe('Locally modified setup');
  });
});
