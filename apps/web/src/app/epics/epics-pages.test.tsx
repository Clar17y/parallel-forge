import { act, cleanup, render, renderHook, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import EpicsPage from './page';
import NewEpicPage from './new/page';
import EpicPage from './[epicId]/page';
import { api, ApiError } from '@/lib/api/client';
import { useEpicMutations , resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';

const mockPush = vi.fn();
const mockReplace = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush, replace: mockReplace }),
  usePathname: () => window.location.pathname,
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) {
      super(code);
    }
  },
}));

describe('Epics App Pages', () => {
  const projectId = 'proj-alpha';
  const epicId = 'epic-alpha-1';

  const mockProjects = [
    {
      id: projectId,
      name: 'Project Alpha',
      created_at: '2026-10-01T00:00:00Z',
      updated_at: '2026-10-01T00:00:00Z',
      active_policy_id: 'pol-1',
    },
  ];

  const mockEpic = {
    epic_id: epicId,
    project_id: projectId,
    title: 'Core Architecture Epic',
    version: 2,
    schema_version: 1,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    draft: {
      problem: 'Need reliable requirements tracking',
      requirements: [],
    },
    accepted_brief_revision_id: 'rev-1',
    accepted_brief_digest: 'd1'.repeat(32),
    accepted_graph_revision_id: null,
    accepted_graph_digest: null,
  };

  beforeEach(() => { resetEpicMutationStoreForTesting();
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  afterEach(() => { resetEpicMutationStoreForTesting();
    cleanup();
    vi.mocked(api).mockReset();
    sessionStorage.clear();
  });

  test('/epics lists epics for selected project and links to workspace', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === '/projects') return mockProjects as T;
      if (path.startsWith('/epics?project_id=')) return [mockEpic] as T;
      return [] as T;
    });

    render(<EpicsPage />);

    expect(await screen.findByText('Core Architecture Epic')).toBeInTheDocument();
    expect(screen.getByText('Brief Accepted')).toBeInTheDocument();
    expect(screen.getByText('No Graph')).toBeInTheDocument();

    const openBtn = screen.getByRole('link', { name: /Open Workspace/i });
    expect(openBtn).toHaveAttribute('href', `/epics/${epicId}`);
  });

  test('/epics explains an empty project list and links to registration', async () => {
    vi.mocked(api).mockResolvedValue([]);
    render(<EpicsPage />);
    expect(await screen.findByText('No projects are registered yet.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Register a project' })).toHaveAttribute('href', '/projects/new');
    expect(api).not.toHaveBeenCalledWith('/epics', expect.anything());
  });

  test('/epics/new allows creating an epic and redirects to its workspace', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === '/projects') return mockProjects as T;
      if (path === '/epics' && init?.method === 'POST') {
        return { ...mockEpic, epic_id: 'new-epic-id-999' } as T;
      }
      return [] as T;
    });

    render(<NewEpicPage />);

    const titleInput = await screen.findByLabelText(/Epic Title/i);
    await userEvent.type(titleInput, 'Brand New Epic');

    const submitBtn = screen.getByRole('button', { name: /Create Epic/i });
    await userEvent.click(submitBtn);

    expect(mockPush).toHaveBeenCalledWith('/epics/new-epic-id-999');
  });

  test('/epics/[epicId] displays epic details and allows switching between workspace sections', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return mockEpic as T;
      if (path === `/epics/${epicId}/brief-revisions`) return [] as T;
      if (path === `/epics/${epicId}/graph-revisions`) return [] as T;
      if (path === `/epics/${epicId}/accepted-brief`) return undefined as T;
      if (path === `/epics/${epicId}/accepted-graph`) return undefined as T;
      return [] as T;
    });

    await act(async () => {
      render(<EpicPage params={Promise.resolve({ epicId })} />);
    });

    expect(await screen.findByText('Core Architecture Epic')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Requirements Brief/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Work-Item Graph/i })).toBeInTheDocument();

    // Switch to Work-Item Graph tab
    const graphTabBtn = screen.getByRole('button', { name: /Work-Item Graph/i });
    await userEvent.click(graphTabBtn);

    expect(screen.getByText(/Work-Item Decomposition/i)).toBeInTheDocument();

    // Switch to Delivery & Progress tab
    const deliveryTabBtn = screen.getByRole('button', { name: /Delivery & Progress/i });
    await userEvent.click(deliveryTabBtn);

    expect(screen.getByRole('heading', { name: /Execution Discovery/i })).toBeInTheDocument();
  });

  test('an uppercase UUID route keeps one workspace query, dirty draft and shared delivery guard', async () => {
    const routeId = 'AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE';
    const canonicalId = routeId.toLowerCase();
    const returned = { ...mockEpic, epic_id: canonicalId, title: 'Canonical epic' };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (init?.method === 'PATCH') throw new Error('reply lost');
      if (path === `/epics/${routeId}` || path === `/epics/${canonicalId}`) return returned as T;
      return [] as T;
    });
    await act(async () => { render(<EpicPage params={Promise.resolve({ epicId: routeId })} />); });
    const title = await screen.findByDisplayValue('Canonical epic');
    const epicReads = () => vi.mocked(api).mock.calls.filter(([path, init]) =>
      [`/epics/${routeId}`, `/epics/${canonicalId}`].includes(path) && !init?.method);
    expect(epicReads()).toHaveLength(1);

    await userEvent.click(screen.getByRole('button', { name: 'Delivery & Progress' }));
    expect(screen.getByRole('button', { name: 'Start Execution' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Requirements Brief' }));
    await userEvent.clear(title);
    await userEvent.type(title, 'Local title retained');
    await userEvent.click(screen.getByRole('button', { name: 'Save draft' }));
    expect(await screen.findByRole('button', { name: 'Retry original request' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Delivery & Progress' }));
    expect(screen.getByRole('button', { name: 'Start Execution' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Requirements Brief' }));
    expect(screen.getByDisplayValue('Local title retained')).toBeInTheDocument();
    expect(epicReads()).toHaveLength(1);
    const writes = vi.mocked(api).mock.calls.filter(([, init]) => init?.method === 'PATCH');
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toBe(`/epics/${routeId}`);
    expect(JSON.parse(writes[0][1]!.body as string).title).toBe('Local title retained');
  });

  test('creation survives a lost response and reload even while project listing is unavailable', async () => {
    let listUnavailable = false;
    let submitted = false;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === '/projects') { if (listUnavailable) throw new Error('offline'); return mockProjects as T; }
      if (path === '/epics' && init?.method === 'POST') {
        if (!submitted) { submitted = true; throw new Error('response lost after create'); }
        return { ...mockEpic, epic_id: 'created-once' } as T;
      }
      return [] as T;
    });
    const first = render(<NewEpicPage />);
    await userEvent.type(await screen.findByLabelText('Epic Title'), 'Create once');
    await userEvent.click(screen.getByRole('button', { name: 'Create Epic' }));
    const original = vi.mocked(api).mock.calls.find(([path, init]) => path === '/epics' && init?.method === 'POST')!;
    first.unmount();
    listUnavailable = true;
    render(<NewEpicPage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry original request' }));
    const creates = vi.mocked(api).mock.calls.filter(([path, init]) => path === '/epics' && init?.method === 'POST');
    expect(creates).toHaveLength(2);
    expect(creates[1]).toEqual(original);
    expect(mockPush).toHaveBeenCalledTimes(1);
    expect(mockPush).toHaveBeenCalledWith('/epics/created-once');
  });

  test('an execution-start retry remains accessible and restores its URL while epic loading is unavailable', async () => {
    vi.mocked(api).mockRejectedValueOnce(new Error('response lost'));
    const first = renderHook(() => useEpicMutations(epicId));
    await act(async () => { await expect(first.result.current.execute('POST', `/epics/${epicId}/executions`, { expected_epic_version: 2 }, { kind: 'execution-start' })).rejects.toThrow(); });
    first.unmount();
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path.endsWith('/executions') && init?.method === 'POST') return { schema_version: 1, execution_id: 'execution-saved-once', execution_version: 1 } as T;
      throw new Error('temporarily unavailable');
    });
    await act(async () => { render(<EpicPage params={Promise.resolve({ epicId })} />); });
    await userEvent.click(await screen.findByRole('button', { name: 'Retry original request' }));
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('execution_id=execution-saved-once'), expect.any(Object));
  });

  test('a definitive retry rejection stays visible while the initial epic read is unavailable', async () => {
    vi.mocked(api).mockRejectedValueOnce(new Error('response lost'));
    const first = renderHook(() => useEpicMutations(epicId));
    await act(async () => { await expect(first.result.current.execute('POST', `/epics/${epicId}/brief-revisions`, {}, { kind: 'brief-revision' })).rejects.toThrow(); });
    first.unmount();
    vi.mocked(api).mockImplementation(async (path: string, init?: RequestInit) => {
      if (path.endsWith('/brief-revisions') && init?.method === 'POST') throw new ApiError(422, 'validation_error', { problem: 'required' });
      throw new Error('temporarily unavailable');
    });
    await act(async () => { render(<EpicPage params={Promise.resolve({ epicId })} />); });
    await userEvent.click(await screen.findByRole('button', { name: 'Retry original request' }));
    expect(await screen.findByText('Validation failed: problem: required')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry original request' })).not.toBeInTheDocument();
    expect(sessionStorage.getItem('epic_pending_mutation_' + epicId)).toBeNull();
  });

  test.each([
    ['conversation-start', `/epics/${epicId}/brainstorm-conversations`, { conversation_id: 'conversation-saved' }, 'conversation_id=conversation-saved'],
    ['job-submit', `/epics/${epicId}/brainstorm-conversations/original-conversation/jobs`, { job_id: 'job-saved' }, 'conversation_id=original-conversation&job_id=job-saved'],
    ['job-retry', `/epics/${epicId}/brainstorm-jobs/original-job/retry`, { job_id: 'original-job' }, 'job_id=original-job'],
  ])('a restored %s receipt retains its subject while initial epic loading is unavailable', async (kind, path, receipt, selection) => {
    vi.mocked(api).mockRejectedValueOnce(new Error('response lost'));
    const first = renderHook(() => useEpicMutations(epicId));
    await act(async () => { await expect(first.result.current.execute('POST', path, {}, { kind })).rejects.toThrow(); });
    first.unmount();
    vi.mocked(api).mockImplementation(async <T,>(requestedPath: string, init?: RequestInit) => {
      if (requestedPath === path && init?.method === 'POST') return receipt as T;
      throw new Error('temporarily unavailable');
    });
    await act(async () => { render(<EpicPage params={Promise.resolve({ epicId })} />); });
    await userEvent.click(await screen.findByRole('button', { name: 'Retry original request' }));
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining(selection), expect.any(Object));
  });
});
