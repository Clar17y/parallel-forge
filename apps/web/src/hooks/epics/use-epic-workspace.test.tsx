import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { renderHook, act, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { useEpicWorkspace } from './use-epic-workspace';
import { api, ApiError } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) {
      super(code);
    }
  },
}));

describe('useEpicWorkspace', () => {
  const epicId = '22222222-2222-4222-8222-222222222222';
  const initialEpic = {
    epic_id: epicId,
    project_id: 'p1',
    title: 'Initial Title',
    version: 1,
    schema_version: 1,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    draft: {
      problem: 'Initial problem',
      outcomes: ['Outcome 1'],
      scope: ['Scope 1'],
      exclusions: [],
      requirements: [{ requirement_id: 'r1', text: 'Req 1', acceptance_criteria: ['Crit 1'] }],
      decisions: ['Decision 1'],
      assumptions: ['Assumption 1'],
      open_questions: ['Question 1'],
      schema_version: 1,
    },
    accepted_brief_revision_id: null,
    accepted_brief_digest: null,
    accepted_graph_revision_id: null,
    accepted_graph_digest: null,
  };

  beforeEach(() => { resetEpicMutationStoreForTesting();
    vi.clearAllMocks();
    vi.mocked(api).mockReset();
    sessionStorage.clear();
  });

  afterEach(() => { resetEpicMutationStoreForTesting();
    sessionStorage.clear();
  });

  test('initializes draft from server and preserves unsaved editor content on polling/external update', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return initialEpic as T;
      if (path === `/epics/${epicId}/brief-revisions`) return [] as T;
      if (path === `/epics/${epicId}/graph-revisions`) return [] as T;
      if (path === `/epics/${epicId}/accepted-brief`) return undefined as T;
      if (path === `/epics/${epicId}/accepted-graph`) return undefined as T;
      throw new Error(`Unexpected path: ${path}`);
    });

    const { result, rerender } = renderHook(() => useEpicWorkspace(epicId));

    // Wait for initial load
    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.draftTitle).toBe('Initial Title');
    expect(result.current.draftContent.problem).toBe('Initial problem');
    expect(result.current.isDirty).toBe(false);

    // Operator edits local draft
    act(() => {
      result.current.updateDraftProblem('Operator edited problem without saving');
    });

    expect(result.current.isDirty).toBe(true);
    expect(result.current.draftContent.problem).toBe('Operator edited problem without saving');

    // Simulate background polling receiving updated server version
    const updatedEpic = {
      ...initialEpic,
      version: 2,
      title: 'Server Updated Title',
      draft: {
        ...initialEpic.draft,
        problem: 'Server updated problem from another tab',
      },
    };

    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return updatedEpic as T;
      return [] as T;
    });

    // Refresh projection
    await act(async () => {
      result.current.refreshEpic();
    });

    // CRITICAL: Local edits must be PRESERVED and not blindly overwritten!
    expect(result.current.draftContent.problem).toBe('Operator edited problem without saving');
    expect(result.current.hasServerConflict).toBe(true);
    expect(result.current.serverVersion).toBe(2);
  });

  test('adopting a changed brief clears accepted graph projections on client', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return initialEpic as T;
      if (path === `/epics/${epicId}/brief-revisions`) return [] as T;
      if (path === `/epics/${epicId}/graph-revisions`) return [] as T;
      return undefined as T;
    });

    const { result } = renderHook(() => useEpicWorkspace(epicId));
    await act(async () => {
      await Promise.resolve();
    });

    // Mock adoption response
    const adoptionReceipt = {
      ...initialEpic,
      version: 2,
      accepted_brief_revision_id: 'rev-brief-1',
      accepted_brief_digest: 'd'.repeat(64),
      accepted_graph_revision_id: null,
      accepted_graph_digest: null,
    };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/brief-adoptions` && init?.method === 'POST') {
        return adoptionReceipt as T;
      }
      if (path === `/epics/${epicId}`) return adoptionReceipt as T;
      return undefined as T;
    });

    await act(async () => {
      await result.current.adoptBrief('rev-brief-1', 'd'.repeat(64));
    });

    expect(result.current.epic?.accepted_brief_revision_id).toBe('rev-brief-1');
    expect(result.current.epic?.accepted_graph_revision_id).toBeNull();
  });

  test('a clean saved draft follows the server version before the next edit', async () => {
    let server = structuredClone(initialEpic);
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (init?.method === 'PATCH') {
        const body = JSON.parse(init.body as string);
        server = { ...server, version: server.version + 1, title: body.title, draft: body.draft };
        return structuredClone(server) as T;
      }
      if (path === `/epics/${epicId}`) return structuredClone(server) as T;
      return [] as T;
    });
    const hook = renderHook(() => useEpicWorkspace(epicId));
    await waitFor(() => expect(hook.result.current.epic?.version).toBe(1));
    act(() => hook.result.current.updateDraftProblem('My edit'));
    await act(async () => { await hook.result.current.saveDraft(); });
    expect(hook.result.current.isDirty).toBe(false);
    server = { ...server, version: 3, title: 'Other tab saved' };
    await act(async () => { hook.result.current.refreshEpic(); });
    act(() => hook.result.current.updateDraftProblem('Next edit'));
    expect(hook.result.current.baseVersion).toBe(3);
    expect(hook.result.current.hasServerConflict).toBe(false);
  });

  test('a saved response updates the authoritative editor even when the follow-up GET fails', async () => {
    let saved = false;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (init?.method === 'PATCH') {
        saved = true;
        const body = JSON.parse(init.body as string);
        return { ...initialEpic, version: 2, draft: body.draft } as T;
      }
      if (path === `/epics/${epicId}`) {
        if (saved) throw new Error('temporary refresh failure');
        return initialEpic as T;
      }
      return [] as T;
    });
    const hook = renderHook(() => useEpicWorkspace(epicId));
    await waitFor(() => expect(hook.result.current.epic).toBeDefined());
    act(() => hook.result.current.updateDraftProblem('Saved content'));
    await act(async () => { await hook.result.current.saveDraft(); });
    expect(hook.result.current.isDirty).toBe(false);
    expect(hook.result.current.draftContent.problem).toBe('Saved content');
    expect(hook.result.current.serverVersion).toBe(2);
    expect(hook.result.current.failed).toBe(true);
  });

  test('a failed poll keeps unsaved content and its original CAS version', async () => {
    let unavailable = false;
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) { if (unavailable) throw new Error('offline'); return initialEpic as T; }
      return [] as T;
    });
    const hook = renderHook(() => useEpicWorkspace(epicId));
    await waitFor(() => expect(hook.result.current.epic).toBeDefined());
    act(() => hook.result.current.updateDraftProblem('Unsaved content'));
    unavailable = true;
    await act(async () => { hook.result.current.refreshEpic(); });
    expect(hook.result.current.epic).toBeDefined();
    expect(hook.result.current.draftContent.problem).toBe('Unsaved content');
    expect(hook.result.current.baseVersion).toBe(1);
  });

  test('clears a previously loaded accepted graph immediately when the brief adoption clears its pointers', async () => {
    const briefId = 'brief-1';
    const digest = 'a'.repeat(64);
    const graph = { graph_revision_id: 'graph-1', graph_digest: 'b'.repeat(64), items: [], readiness: [] };
    let server = { ...initialEpic, accepted_brief_revision_id: briefId as string | null, accepted_brief_digest: digest as string | null, accepted_graph_revision_id: 'graph-1' as string | null, accepted_graph_digest: graph.graph_digest as string | null };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.endsWith('/brief-adoptions') && init?.method === 'POST') {
        server = { ...server, version: 2, accepted_brief_revision_id: 'brief-2', accepted_brief_digest: 'c'.repeat(64), accepted_graph_revision_id: null, accepted_graph_digest: null };
        return server as T;
      }
      if (path === `/epics/${epicId}`) return server as T;
      if (path.endsWith('/accepted-graph')) { if (!server.accepted_graph_revision_id) throw new ApiError(409, 'no-accepted-graph'); return graph as T; }
      if (path.endsWith('/accepted-brief')) return { brief_revision_id: server.accepted_brief_revision_id, brief_digest: server.accepted_brief_digest, requirements: [] } as T;
      return [] as T;
    });
    const hook = renderHook(() => useEpicWorkspace(epicId));
    await waitFor(() => expect(hook.result.current.acceptedGraph?.graph_revision_id).toBe('graph-1'));
    await act(async () => { await hook.result.current.adoptBrief('brief-2', 'c'.repeat(64)); });
    expect(hook.result.current.acceptedGraph).toBeUndefined();
    expect(hook.result.current.epic?.accepted_graph_revision_id).toBeNull();
  });

  test('replaying an older draft does not clear newer local edits', async () => {
    let attempts = 0;
    let server = structuredClone(initialEpic);
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (init?.method === 'PATCH') {
        if (++attempts === 1) throw new Error('response lost');
        const body = JSON.parse(init.body as string);
        server = { ...server, version: 2, draft: body.draft };
        return server as T;
      }
      return (path === `/epics/${epicId}` ? server : []) as T;
    });
    const hook = renderHook(() => useEpicWorkspace(epicId));
    await waitFor(() => expect(hook.result.current.epic).toBeDefined());
    act(() => hook.result.current.updateDraftProblem('Submitted content'));
    await act(async () => { await expect(hook.result.current.saveDraft()).rejects.toThrow(); });
    act(() => hook.result.current.updateDraftProblem('Newer local content'));
    await act(async () => { await hook.result.current.mutations.retryPending(); });
    expect(hook.result.current.draftContent.problem).toBe('Newer local content');
    expect(hook.result.current.isDirty).toBe(true);
    expect(hook.result.current.baseVersion).toBe(2);
    expect(server.draft.problem).toBe('Submitted content');
  });

  test('readopting the same brief retains its graph but a mismatched projection is never accepted', async () => {
    const digest = 'a'.repeat(64);
    const graph = { graph_revision_id: 'graph-1', graph_digest: 'b'.repeat(64), items: [], readiness: [] };
    let server = { ...initialEpic, accepted_brief_revision_id: 'brief-1', accepted_brief_digest: digest, accepted_graph_revision_id: 'graph-1', accepted_graph_digest: graph.graph_digest };
    let graphDigest = graph.graph_digest;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.endsWith('/brief-adoptions') && init?.method === 'POST') { server = { ...server, version: 2 }; return server as T; }
      if (path === `/epics/${epicId}`) return server as T;
      if (path.endsWith('/accepted-graph')) return { ...graph, graph_digest: graphDigest } as T;
      if (path.endsWith('/accepted-brief')) return { brief_revision_id: 'brief-1', brief_digest: digest, requirements: [] } as T;
      return [] as T;
    });
    const hook = renderHook(() => useEpicWorkspace(epicId));
    await waitFor(() => expect(hook.result.current.acceptedGraph?.graph_revision_id).toBe('graph-1'));
    await act(async () => { await hook.result.current.adoptBrief('brief-1', digest); });
    expect(hook.result.current.acceptedGraph?.graph_revision_id).toBe('graph-1');
    graphDigest = 'c'.repeat(64);
    await act(async () => { hook.result.current.refreshProjections(); });
    expect(hook.result.current.acceptedGraph).toBeUndefined();
  });
});
