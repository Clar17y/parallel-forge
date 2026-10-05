import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { BriefEditor } from './brief-editor';
import { api, ApiError } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) {
      super(code);
    }
  },
}));

describe('BriefEditor', () => {
  const epicId = '11111111-2222-3333-4444-555555555555';
  const initialEpic = {
    epic_id: epicId,
    project_id: 'proj-1',
    title: 'Workspace Architecture Epic',
    version: 3,
    schema_version: 1,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    draft: {
      schema_version: 1,
      problem: 'Need a dedicated workspace for epics',
      outcomes: ['Operators can manage epics reliably'],
      scope: ['Web client UI and API interaction'],
      exclusions: ['No generic backend redesign'],
      requirements: [
        {
          requirement_id: 'req-stable-1',
          text: 'Stable requirement text',
          acceptance_criteria: ['Criterion A', 'Criterion B'],
        },
      ],
      decisions: ['Confirmed Decision 1: Use Postgres as system of record'],
      assumptions: ['Assumption 1: User runs modern browser'],
      open_questions: ['Open Question 1: How to handle offline mode?'],
    },
    accepted_brief_revision_id: 'rev-accepted-1',
    accepted_brief_digest: 'a'.repeat(64),
    accepted_graph_revision_id: null,
    accepted_graph_digest: null,
  };

  const initialRevisions = [
    {
      schema_version: 1,
      brief_revision_id: 'rev-accepted-1',
      revision_number: 1,
      epic_id: epicId,
      epic_version: 2,
      content: initialEpic.draft,
      content_digest: 'a'.repeat(64),
      created_at: '2026-10-01T01:00:00Z',
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

  test('renders brief draft, visually distinguishes decisions, assumptions, and open questions', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return initialEpic as T;
      if (path === `/epics/${epicId}/brief-revisions`) return initialRevisions as T;
      if (path === `/epics/${epicId}/accepted-brief`) return { ...initialEpic.draft, brief_revision_id: 'rev-accepted-1', brief_digest: 'a'.repeat(64) } as T;
      return [] as T;
    });

    render(<BriefEditor epicId={epicId} />);

    expect(await screen.findByDisplayValue('Workspace Architecture Epic')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Need a dedicated workspace for epics')).toBeInTheDocument();

    // Verify visually distinct sections
    const decisionsSection = screen.getByTestId('brief-decisions');
    expect(decisionsSection).toBeInTheDocument();
    expect(screen.getByDisplayValue('Confirmed Decision 1: Use Postgres as system of record')).toBeInTheDocument();

    const assumptionsSection = screen.getByTestId('brief-assumptions');
    expect(assumptionsSection).toBeInTheDocument();
    expect(screen.getByDisplayValue('Assumption 1: User runs modern browser')).toBeInTheDocument();

    const questionsSection = screen.getByTestId('brief-open-questions');
    expect(questionsSection).toBeInTheDocument();
    expect(screen.getByDisplayValue('Open Question 1: How to handle offline mode?')).toBeInTheDocument();

    // Requirements display stable ID and criteria
    expect(screen.getByDisplayValue('Stable requirement text')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Criterion A')).toBeInTheDocument();
  });

  test('preserves stable requirement ID when modifying requirement text and criteria', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return initialEpic as T;
      if (path === `/epics/${epicId}/brief-revisions`) return initialRevisions as T;
      return [] as T;
    });

    render(<BriefEditor epicId={epicId} />);
    const reqInput = await screen.findByDisplayValue('Stable requirement text');

    await userEvent.clear(reqInput);
    await userEvent.type(reqInput, 'Updated requirement text');

    // Mock save draft
    vi.mocked(api).mockResolvedValueOnce({
      ...initialEpic,
      version: 4,
    });

    await userEvent.click(screen.getByRole('button', { name: /Save draft/i }));

    expect(api).toHaveBeenCalledWith(
      `/epics/${epicId}`,
      expect.objectContaining({
        method: 'PATCH',
        body: expect.stringContaining('req-stable-1'),
      })
    );
  });

  test('allows saving new revision and adopting revision with exact ID and digest', async () => {
    const revisionsWithDraft = [
      ...initialRevisions,
      {
        schema_version: 1,
        brief_revision_id: 'rev-draft-2',
        revision_number: 2,
        epic_id: epicId,
        epic_version: 3,
        content: initialEpic.draft,
        content_digest: 'b'.repeat(64),
        created_at: '2026-10-01T02:00:00Z',
      },
    ];

    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/brief-adoptions` && init?.method === 'POST') {
        return {
          ...initialEpic,
          version: 4,
          accepted_brief_revision_id: 'rev-draft-2',
          accepted_brief_digest: 'b'.repeat(64),
        } as T;
      }
      if (path === `/epics/${epicId}`) return initialEpic as T;
      if (path === `/epics/${epicId}/brief-revisions`) return revisionsWithDraft as T;
      return [] as T;
    });

    render(<BriefEditor epicId={epicId} />);

    // Switch to history tab/view
    const historyBtn = await screen.findByRole('button', { name: /History & Revisions/i });
    await userEvent.click(historyBtn);

    const adoptBtn = screen.getByRole('button', { name: /Adopt revision #2/i });
    expect(adoptBtn).toBeInTheDocument();
    await userEvent.click(adoptBtn);

    expect(api).toHaveBeenCalledWith(
      `/epics/${epicId}/brief-adoptions`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          schema_version: 1,
          expected_epic_version: 3,
          brief_revision_id: 'rev-draft-2',
          brief_digest: 'b'.repeat(64),
        }),
      })
    );
  });

  test('the accepted baseline shows outcomes, scope and exclusions alongside requirements', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return initialEpic as T;
      if (path.endsWith('/accepted-brief')) return { ...initialEpic.draft, brief_revision_id: 'rev-accepted-1', brief_digest: 'a'.repeat(64) } as T;
      return [] as T;
    });
    render(<BriefEditor epicId={epicId} />);
    await screen.findByDisplayValue('Workspace Architecture Epic');
    await userEvent.click(screen.getByRole('button', { name: 'Accepted Brief' }));
    expect(await screen.findByText(initialEpic.draft.outcomes[0])).toBeInTheDocument();
    expect(screen.getByText(initialEpic.draft.scope[0])).toBeInTheDocument();
    expect(screen.getByText(initialEpic.draft.exclusions[0])).toBeInTheDocument();
  });

  test('historical evidence exposes the saved content rather than the current draft', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return initialEpic as T;
      if (path.endsWith('/brief-revisions')) return [{ schema_version: 1, epic_id: epicId, epic_version: 1, brief_revision_id: 'historical', revision_number: 1, content_digest: 'h'.repeat(64), created_at: '2026-10-01T00:00:00Z', content: { ...initialEpic.draft, requirements: [{ requirement_id: 'saved-req', text: 'Saved requirement', acceptance_criteria: ['Historical criterion'] }] } }] as T;
      return [] as T;
    });
    render(<BriefEditor epicId={epicId} />);
    await screen.findByDisplayValue('Workspace Architecture Epic');
    await userEvent.click(screen.getByRole('button', { name: /History & Revisions/ }));
    await userEvent.click(screen.getByText('Inspect revision IDs and digest'));
    expect(await screen.findByText(/Historical criterion/)).toBeVisible();
  });

  test('failed history loading is distinct from having no saved revisions', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return initialEpic as T;
      if (path.endsWith('/brief-revisions')) throw new Error('temporary read failure');
      return [] as T;
    });
    render(<BriefEditor epicId={epicId} />);
    await screen.findByDisplayValue('Workspace Architecture Epic');
    await userEvent.click(screen.getByRole('button', { name: /History & Revisions/ }));
    expect(screen.getByText('Brief history could not be loaded.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry brief history' })).toBeEnabled();
    expect(screen.queryByText('No brief revisions saved yet.')).not.toBeInTheDocument();
  });

  test('keeps a changed title and requires saving it before a content-only revision', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}` && init?.method === 'PATCH') {
        const request = JSON.parse(init.body as string);
        return { ...initialEpic, version: 4, title: request.title, draft: request.draft } as T;
      }
      if (path === `/epics/${epicId}`) return initialEpic as T;
      return [] as T;
    });
    render(<BriefEditor epicId={epicId} />);
    const title = await screen.findByDisplayValue(initialEpic.title);
    await userEvent.clear(title);
    await userEvent.type(title, 'My saved title');
    expect(screen.getByRole('button', { name: 'Save as new revision' })).toBeDisabled();
    expect(screen.getByDisplayValue('My saved title')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Save draft' }));
    expect(screen.getByRole('button', { name: 'Save as new revision' })).toBeEnabled();
    expect(screen.getByDisplayValue('My saved title')).toBeInTheDocument();
  });

  test('reports neutral truthful wording when epic version changes on server while local draft is dirty', async () => {
    let server = initialEpic;
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      if (path === `/epics/${epicId}`) return server as T;
      return [] as T;
    });
    render(<BriefEditor epicId={epicId} />);
    const title = await screen.findByDisplayValue(initialEpic.title);
    await userEvent.clear(title);
    await userEvent.type(title, 'Local unsaved change');

    server = { ...initialEpic, version: 5 };
    await act(async () => {
      window.dispatchEvent(new StorageEvent('storage', { key: `epic_refresh_${epicId}`, newValue: 'other-tab:5' }));
    });

    expect(await screen.findByText(/The epic version changed on the server \(Server version: 5, Local base: 3\)/)).toBeInTheDocument();
    expect(screen.queryByText(/another session/i)).not.toBeInTheDocument();
  });

  test('shows brief version conflict only for brief mutations', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}` && init?.method === 'PATCH') {
        throw new ApiError(409, 'version-conflict');
      }
      if (path === `/epics/${epicId}`) return initialEpic as T;
      return [] as T;
    });
    render(<BriefEditor epicId={epicId} />);
    const title = await screen.findByDisplayValue(initialEpic.title);
    await userEvent.clear(title);
    await userEvent.type(title, 'Attempt save');
    await userEvent.click(screen.getByRole('button', { name: 'Save draft' }));

    expect(await screen.findByText('Epic version conflict: The epic version changed on the server before saving.')).toBeInTheDocument();
  });
});
