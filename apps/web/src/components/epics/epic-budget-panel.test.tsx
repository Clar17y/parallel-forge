import { resetEpicMutationStoreForTesting } from '@/hooks/epics/use-epic-mutations';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { EpicBudgetPanel } from './epic-budget-panel';
import { api, ApiError } from '@/lib/api/client';
import type { EpicBudgetProjection } from '@/hooks/epics/types';

vi.mock('@/lib/api/client', () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) { super(code); }
  },
}));

describe('EpicBudgetPanel', () => {
  const epicId = '11111111-1111-4111-8111-111111111111';

  const mockBudget: EpicBudgetProjection = {
    epic_id: epicId,
    initialized: true,
    version: 3,
    ceiling: {
      billing_mode: 'allowance_only',
      max_duration_seconds: 1800,
      max_cost_minor: 500,
      max_input_tokens: 100000,
      max_output_tokens: 20000,
      max_tool_calls: 50,
      max_provider_attempts: 3,
      max_named_checks: 10,
      max_repairs: 3,
    },
    disabled_dimensions: ['duration_ms'],
    known: {
      duration_ms: 12000,
      tool_call_count: 0,
      input_tokens: 4500,
      output_tokens: 800,
      estimated_api_cost_minor: 0,
      provider_attempts: 1,
    },
    held: {
      duration_ms: 0,
      tool_call_count: 2,
      input_tokens: 5000,
      output_tokens: 1000,
      estimated_api_cost_minor: 25,
      provider_attempts: 0,
    },
    unknown: true,
    currency: 'USD',
    warnings: ['Cost minor is approaching warning threshold'],
    permits: [
      {
        permit_id: 'p-1',
        run_id: 'run-alpha',
        actor_id: 'act-1',
        consumed_attempt_id: null,
        note: 'Prior admission permit',
        warnings: [],
      },
    ],
    owner_actions: [
      {
        actor_id: 'act-1',
        event_type: 'edit_budget',
        version: 2,
        note: 'Initial budget setup',
      },
    ],
  };

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

  test('truthfully displays 6 shared dimensions, distinguishing zero, unknown, and unlimited', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) return mockBudget as T;
      return undefined as T;
    });

    render(<EpicBudgetPanel epicId={epicId} />);
    expect(await screen.findByText('Shared Epic Budget & Ceilings')).toBeInTheDocument();

    // Duration is in disabled_dimensions -> Unlimited
    expect(screen.getByTestId('ceiling-duration_ms')).toHaveTextContent(/Unlimited/i);

    // Tool call known is 0 -> distinct 0
    expect(screen.getByTestId('known-tool_call_count')).toHaveTextContent('0');

    // Cost ceiling is 500 minor units
    expect(screen.getByTestId('ceiling-estimated_api_cost_minor')).toHaveTextContent('500 minor units');

    // Unknown exposure note
    expect(screen.getByText(/Some usage is unknown. Unknown usage is not zero./i)).toBeInTheDocument();

    // Warnings
    expect(screen.getByText('Cost minor is approaching warning threshold')).toBeInTheDocument();

    // Currency
    expect(screen.getByText(/Currency: USD/i)).toBeInTheDocument();
  });

  test('allows owner to edit ceilings, unset a dimension to unlimited, and provide an optional note', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) return mockBudget as T;
      if (path === `/epics/${epicId}/budget` && init?.method === 'PUT') {
        return {
          epic_id: epicId,
          version: 4,
          ceiling: { ...mockBudget.ceiling, max_cost_minor: 1200 },
          disabled_dimensions: ['duration_ms', 'output_tokens'],
        } as T;
      }
      return undefined as T;
    });

    render(<EpicBudgetPanel epicId={epicId} />);
    await screen.findByText('Shared Epic Budget & Ceilings');

    // Open edit form
    await userEvent.click(screen.getByRole('button', { name: /Edit Budget Ceilings/i }));

    // Change cost limit
    const costInput = screen.getByLabelText(/Cost ceiling \(minor units\)/i);
    await userEvent.clear(costInput);
    await userEvent.type(costInput, '1200');

    // Toggle output tokens to Unlimited
    const outputUnlimitedCheckbox = screen.getByLabelText(/Unlimited output tokens/i);
    await userEvent.click(outputUnlimitedCheckbox);

    // Provide note
    const noteInput = screen.getByLabelText(/Optional owner note/i);
    await userEvent.type(noteInput, 'Raised cost ceiling to 1200 and unset output tokens');

    // Submit
    await userEvent.click(screen.getByRole('button', { name: /Save Ceilings/i }));

    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/budget`, expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({
        ceiling: {
          ...mockBudget.ceiling,
          max_cost_minor: 1200,
        },
        expected_version: 3,
        disabled_dimensions: ['duration_ms', 'output_tokens'],
        note: 'Raised cost ceiling to 1200 and unset output tokens',
      }),
    }));
  });

  test('allows owner to request admission permit with run ID and optional note', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) return mockBudget as T;
      if (path === `/epics/${epicId}/budget/admissions` && init?.method === 'POST') {
        return {
          permit_id: 'p-new',
          run_id: '88888888-8888-4888-8888-888888888888',
          epic_id: epicId,
          actor_id: 'act-1',
          budget_version: 4,
          warnings: ['Permit granted beyond default ceiling'],
        } as T;
      }
      return undefined as T;
    });

    render(<EpicBudgetPanel epicId={epicId} />);
    await screen.findByText('Shared Epic Budget & Ceilings');

    // Open permit section
    await userEvent.click(screen.getByRole('button', { name: /Request Admission Permit/i }));

    const runIdInput = screen.getByLabelText(/Child Run ID/i);
    await userEvent.type(runIdInput, '88888888-8888-4888-8888-888888888888');

    const permitNoteInput = screen.getByLabelText(/Permit note \(optional\)/i);
    await userEvent.type(permitNoteInput, 'Authorized run permit for urgent repair');

    await userEvent.click(screen.getByRole('button', { name: /Issue Permit/i }));

    expect(api).toHaveBeenCalledWith(`/epics/${epicId}/budget/admissions`, expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        run_id: '88888888-8888-4888-8888-888888888888',
        expected_version: 3,
        note: 'Authorized run permit for urgent repair',
      }),
    }));
  });

  test('rebases onto a completed fresh read while preserving edits and unrelated owner changes', async () => {
    const latestBudget: EpicBudgetProjection = {
      ...mockBudget,
      version: 4,
      ceiling: {
        ...mockBudget.ceiling,
        max_cost_minor: 800,
        max_tool_calls: 70,
        max_input_tokens: null,
        max_output_tokens: 40000,
        max_repairs: 7,
      },
      disabled_dimensions: ['tool_call_count', 'input_tokens'],
    };
    let completeRead!: (value: EpicBudgetProjection) => void;
    const freshRead = new Promise<EpicBudgetProjection>(resolve => { completeRead = resolve; });
    let reads = 0;
    let writes = 0;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) {
        reads += 1;
        return (reads === 1 ? mockBudget : await freshRead) as T;
      }
      if (path === `/epics/${epicId}/budget` && init?.method === 'PUT') {
        writes += 1;
        if (writes === 1) throw new ApiError(409, 'stale-projection');
        return { epic_id: epicId, version: 5, warnings: [] } as T;
      }
      return undefined as T;
    });

    render(<EpicBudgetPanel epicId={epicId} />);
    await screen.findByText('Shared Epic Budget & Ceilings');

    await userEvent.click(screen.getByRole('button', { name: /Edit Budget Ceilings/i }));

    const costInput = screen.getByLabelText(/Cost ceiling \(minor units\)/i);
    await userEvent.clear(costInput);
    await userEvent.type(costInput, '999');
    await userEvent.click(screen.getByLabelText(/Unlimited output tokens/i));
    await userEvent.type(screen.getByLabelText(/Optional owner note/i), 'Preserve this owner edit');

    await userEvent.click(screen.getByRole('button', { name: /Save Ceilings/i }));

    expect(await screen.findByText(/Budget version conflict/i)).toBeInTheDocument();
    // User edit is preserved in the input
    expect(screen.getByLabelText(/Cost ceiling \(minor units\)/i)).toHaveValue(999);
    await userEvent.click(screen.getByRole('button', { name: /Rebase changes onto latest version/i }));
    expect(screen.getByRole('button', { name: /Save Ceilings/i })).toBeDisabled();
    expect(screen.getByText(/Budget version conflict/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Cost ceiling \(minor units\)/i)).toHaveValue(999);
    expect(screen.getByLabelText(/Optional owner note/i)).toHaveValue('Preserve this owner edit');

    await act(async () => { completeRead(latestBudget); });
    await waitFor(() => {
      expect(screen.queryByText(/Budget version conflict/i)).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Save Ceilings/i })).toBeEnabled();
    });
    expect(screen.getByLabelText(/Tool calls ceiling/i)).toHaveValue(70);
    expect(screen.getByLabelText(/Unlimited tool calls/i)).toBeChecked();
    expect(screen.getByLabelText(/Unlimited duration/i)).not.toBeChecked();
    expect(screen.getByLabelText(/Unlimited output tokens/i)).toBeChecked();

    await userEvent.click(screen.getByRole('button', { name: /Save Ceilings/i }));
    const putCalls = vi.mocked(api).mock.calls.filter(([, init]) => init?.method === 'PUT');
    expect(putCalls).toHaveLength(2);
    const body = JSON.parse(putCalls[1][1]?.body as string);
    expect(body.expected_version).toBe(4);
    expect(body.ceiling).toEqual({ ...latestBudget.ceiling, max_cost_minor: 999 });
    expect(body.disabled_dimensions.sort()).toEqual(['input_tokens', 'output_tokens', 'tool_call_count']);
    expect(body.note).toBe('Preserve this owner edit');
    await waitFor(() => expect(screen.queryByLabelText(/Cost ceiling \(minor units\)/i)).not.toBeInTheDocument());
  });

  test.each(['failed', 'empty'])('preserves conflict and edits after a %s fresh read, then allows another rebase', async failure => {
    const latestBudget = { ...mockBudget, version: 4 };
    let reads = 0;
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) {
        reads += 1;
        if (reads === 2) {
          if (failure === 'failed') throw new Error('offline');
          return undefined;
        }
        return (reads === 1 ? mockBudget : latestBudget) as T;
      }
      if (path === `/epics/${epicId}/budget` && init?.method === 'PUT') {
        throw new ApiError(409, 'stale-projection');
      }
      return undefined as T;
    });

    render(<EpicBudgetPanel epicId={epicId} />);
    await screen.findByText('Shared Epic Budget & Ceilings');
    await userEvent.click(screen.getByRole('button', { name: /Edit Budget Ceilings/i }));
    await userEvent.clear(screen.getByLabelText(/Cost ceiling \(minor units\)/i));
    await userEvent.type(screen.getByLabelText(/Cost ceiling \(minor units\)/i), '999');
    await userEvent.type(screen.getByLabelText(/Optional owner note/i), 'Keep my note');
    await userEvent.click(screen.getByRole('button', { name: /Save Ceilings/i }));
    await screen.findByText(/Budget version conflict/i);

    await userEvent.click(screen.getByRole('button', { name: /Rebase changes onto latest version/i }));
    expect(await screen.findByText(/Could not load the latest budget/i)).toBeInTheDocument();
    expect(screen.getByText(/Budget version conflict/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Cost ceiling \(minor units\)/i)).toHaveValue(999);
    expect(screen.getByLabelText(/Optional owner note/i)).toHaveValue('Keep my note');

    await userEvent.click(screen.getByRole('button', { name: /Rebase changes onto latest version/i }));
    await waitFor(() => {
      expect(screen.queryByText(/Budget version conflict/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/Could not load the latest budget/i)).not.toBeInTheDocument();
    });
    await userEvent.click(screen.getByRole('button', { name: /Save Ceilings/i }));
    const writes = vi.mocked(api).mock.calls.filter(([, init]) => init?.method === 'PUT');
    expect(JSON.parse(writes[1][1]?.body as string)).toMatchObject({
      expected_version: 4,
      ceiling: { max_cost_minor: 999 },
      note: 'Keep my note',
    });
  });

  test('preserves untouched null/unlimited ceilings when editing an unrelated dimension', async () => {
    const budgetWithNulls: EpicBudgetProjection = {
      ...mockBudget,
      ceiling: {
        ...mockBudget.ceiling,
        max_input_tokens: null,
        max_output_tokens: null,
      },
      disabled_dimensions: [],
    };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) return budgetWithNulls as T;
      if (path === `/epics/${epicId}/budget` && init?.method === 'PUT') {
        return { epic_id: epicId, version: 4, ceiling: budgetWithNulls.ceiling, disabled_dimensions: [] } as T;
      }
      return undefined as T;
    });

    render(<EpicBudgetPanel epicId={epicId} />);
    await screen.findByText('Shared Epic Budget & Ceilings');

    // Untouched input tokens displays Unlimited (null)
    expect(screen.getByTestId('ceiling-input_tokens')).toHaveTextContent(/Unlimited/i);

    await userEvent.click(screen.getByRole('button', { name: /Edit Budget Ceilings/i }));

    // User only changes max_duration_seconds
    const durationInput = screen.getByLabelText(/Duration ceiling \(seconds\)/i);
    await userEvent.clear(durationInput);
    await userEvent.type(durationInput, '3600');

    await userEvent.click(screen.getByRole('button', { name: /Save Ceilings/i }));

    const putCalls = vi.mocked(api).mock.calls.filter(([p, init]) => p === `/epics/${epicId}/budget` && init?.method === 'PUT');
    expect(putCalls).toHaveLength(1);
    const body = JSON.parse(putCalls[0][1]?.body as string);
    // Crucial: untouched max_input_tokens and max_output_tokens must remain null!
    expect(body.ceiling.max_input_tokens).toBeNull();
    expect(body.ceiling.max_output_tokens).toBeNull();
    expect(body.ceiling.max_duration_seconds).toBe(3600);
  });

  test('preserves numeric ceiling when disabled and re-enabled', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) return mockBudget as T;
      return undefined as T;
    });

    render(<EpicBudgetPanel epicId={epicId} />);
    await screen.findByText('Shared Epic Budget & Ceilings');

    await userEvent.click(screen.getByRole('button', { name: /Edit Budget Ceilings/i }));

    // Tool calls initial ceiling is 50
    const toolCallInput = screen.getByLabelText(/Tool calls ceiling/i);
    expect(toolCallInput).toHaveValue(50);

    // Toggle unlimited (disabled)
    const unlimitedToggle = screen.getByLabelText(/Unlimited tool calls/i);
    await userEvent.click(unlimitedToggle);
    expect(toolCallInput).toBeDisabled();

    // Toggle back to enabled
    await userEvent.click(unlimitedToggle);
    expect(toolCallInput).not.toBeDisabled();
    // Numeric value 50 must still be preserved!
    expect(toolCallInput).toHaveValue(50);
  });

  test('allows an explicit null token ceiling and preserves the other token cap', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) return mockBudget as T;
      if (path === `/epics/${epicId}/budget` && init?.method === 'PUT') return { ...mockBudget, version: 4 } as T;
      return undefined as T;
    });
    render(<EpicBudgetPanel epicId={epicId} />);
    await screen.findByText('Shared Epic Budget & Ceilings');
    await userEvent.click(screen.getByRole('button', { name: /Edit Budget Ceilings/i }));
    await userEvent.click(screen.getByRole('button', { name: 'Input tokens ceiling use unlimited' }));
    expect(screen.queryByRole('slider', { name: 'Input tokens ceiling' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Save Ceilings/i }));
    const put = vi.mocked(api).mock.calls.find(([path, init]) => path === `/epics/${epicId}/budget` && init?.method === 'PUT');
    const body = JSON.parse(put![1]?.body as string);
    expect(body.ceiling.max_input_tokens).toBeNull();
    expect(body.ceiling.max_output_tokens).toBe(20000);
  });

  test('formats duration ceiling in seconds without dividing by 1000 or labeling as ms', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) {
        return {
          ...mockBudget,
          disabled_dimensions: [], // enabled
        } as T;
      }
      return undefined as T;
    });

    render(<EpicBudgetPanel epicId={epicId} />);
    await screen.findByText('Shared Epic Budget & Ceilings');

    // 1800 seconds duration ceiling should be formatted as 1800s, not 2s or 1800ms
    const durationCeiling = screen.getByTestId('ceiling-duration_ms');
    expect(durationCeiling).toHaveTextContent('1800s');
    expect(durationCeiling).not.toHaveTextContent('1800ms');
    expect(durationCeiling).not.toHaveTextContent('2s');
  });

  test('keeps budget CAS bound to source version at editor open across concurrent polling updates', async () => {
    let currentBudget = { ...mockBudget, version: 3 };
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) return currentBudget as T;
      if (path === `/epics/${epicId}/budget` && init?.method === 'PUT') {
        const body = JSON.parse(init.body as string);
        if (body.expected_version !== currentBudget.version) {
          throw new ApiError(409, 'stale-projection');
        }
        return { epic_id: epicId, version: 5, ceiling: mockBudget.ceiling, disabled_dimensions: [] } as T;
      }
      return undefined as T;
    });

    render(<EpicBudgetPanel epicId={epicId} />);
    await screen.findByText('Shared Epic Budget & Ceilings');

    // Open edit form when version is 3
    await userEvent.click(screen.getByRole('button', { name: /Edit Budget Ceilings/i }));

    // Simulate background poll updating version to 4
    currentBudget = { ...mockBudget, version: 4 };
    await userEvent.click(screen.getByRole('button', { name: /Refresh Budget/i }));
    await screen.findByText(/Version 4/);

    // Modify a ceiling
    const costInput = screen.getByLabelText(/Cost ceiling \(minor units\)/i);
    await userEvent.clear(costInput);
    await userEvent.type(costInput, '800');

    // Save should still send expected_version: 3 (source version at editor open, preventing silent overwrite)
    await userEvent.click(screen.getByRole('button', { name: /Save Ceilings/i }));

    const putCalls = vi.mocked(api).mock.calls.filter(([p, init]) => p === `/epics/${epicId}/budget` && init?.method === 'PUT');
    expect(putCalls).toHaveLength(1);
    const body = JSON.parse(putCalls[0][1]?.body as string);
    expect(body.expected_version).toBe(3);
    expect(await screen.findByText(/Budget version conflict/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Cost ceiling \(minor units\)/i)).toHaveValue(800);
  });

  test('requires numeric cap for enabled mandatory dimensions with inline error and blocks submit on blank, while explicit zero retains zero', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) {
        return {
          ...mockBudget,
          disabled_dimensions: [], // All enabled
        } as T;
      }
      if (path === `/epics/${epicId}/budget` && init?.method === 'PUT') {
        return { epic_id: epicId, version: 4 } as T;
      }
      return undefined as T;
    });

    render(<EpicBudgetPanel epicId={epicId} />);
    await screen.findByText('Shared Epic Budget & Ceilings');

    await userEvent.click(screen.getByRole('button', { name: /Edit Budget Ceilings/i }));

    const durationInput = screen.getByLabelText(/Duration ceiling \(seconds\)/i);
    await userEvent.clear(durationInput);

    // Should display inline error for mandatory dimension when blank and enabled
    expect(await screen.findByText(/Numeric ceiling is required when enabled/i)).toBeInTheDocument();

    // Clicking Save Ceilings must not call PUT API for blank mandatory ceiling
    await userEvent.click(screen.getByRole('button', { name: /Save Ceilings/i }));
    const putCalls = vi.mocked(api).mock.calls.filter(([p, init]) => p === `/epics/${epicId}/budget` && init?.method === 'PUT');
    expect(putCalls).toHaveLength(0);

    // Explicit zero retains zero semantics
    await userEvent.type(durationInput, '0');
    expect(screen.queryByText(/Numeric ceiling is required when enabled/i)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Save Ceilings/i }));
    const putCallsAfterZero = vi.mocked(api).mock.calls.filter(([p, init]) => p === `/epics/${epicId}/budget` && init?.method === 'PUT');
    expect(putCallsAfterZero).toHaveLength(1);
    const body = JSON.parse(putCallsAfterZero[0][1]?.body as string);
    expect(body.ceiling.max_duration_seconds).toBe(0);
  });

  test('retains stored mandatory numeric ceiling when input is blank and Unlimited is checked', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) {
        return {
          ...mockBudget,
          ceiling: {
            ...mockBudget.ceiling,
            max_duration_seconds: 1800,
          },
          disabled_dimensions: [], // currently enabled with 1800
        } as T;
      }
      if (path === `/epics/${epicId}/budget` && init?.method === 'PUT') {
        return { epic_id: epicId, version: 4 } as T;
      }
      return undefined as T;
    });

    render(<EpicBudgetPanel epicId={epicId} />);
    await screen.findByText('Shared Epic Budget & Ceilings');

    await userEvent.click(screen.getByRole('button', { name: /Edit Budget Ceilings/i }));

    const durationInput = screen.getByLabelText(/Duration ceiling \(seconds\)/i);
    await userEvent.clear(durationInput);
    expect(await screen.findByText(/Numeric ceiling is required when enabled/i)).toBeInTheDocument();

    // Check Unlimited duration -> disables dimension
    await userEvent.click(screen.getByLabelText(/Unlimited duration/i));
    // Error is dismissed because dimension is disabled
    expect(screen.queryByText(/Numeric ceiling is required when enabled/i)).not.toBeInTheDocument();

    // Save with blank input while disabled: must retain the stored mandatory ceiling (1800), not coerce to 0!
    await userEvent.click(screen.getByRole('button', { name: /Save Ceilings/i }));
    const putCalls = vi.mocked(api).mock.calls.filter(([p, init]) => p === `/epics/${epicId}/budget` && init?.method === 'PUT');
    expect(putCalls).toHaveLength(1);
    const body = JSON.parse(putCalls[0][1]?.body as string);
    expect(body.disabled_dimensions).toContain('duration_ms');
    expect(body.ceiling.max_duration_seconds).toBe(1800);
  });

  test('renders truthful placeholders distinguishing mandatory from nullable dimensions', async () => {
    vi.mocked(api).mockImplementation(async <T,>(path: string, init?: RequestInit) => {
      if (path === `/epics/${epicId}/budget` && (!init?.method || init.method === 'GET')) {
        return {
          ...mockBudget,
          ceiling: {
            ...mockBudget.ceiling,
            max_duration_seconds: 1800,
            max_input_tokens: null,
          },
          disabled_dimensions: [],
        } as T;
      }
      return undefined as T;
    });

    render(<EpicBudgetPanel epicId={epicId} />);
    await screen.findByText('Shared Epic Budget & Ceilings');

    await userEvent.click(screen.getByRole('button', { name: /Edit Budget Ceilings/i }));

    const durationInput = screen.getByLabelText(/Duration ceiling \(seconds\)/i);
    await userEvent.clear(durationInput);
    // Mandatory duration placeholder when enabled and blank must NOT claim "Unlimited (null)"
    expect(durationInput).toHaveAttribute('placeholder', 'Required numeric cap');

    expect(screen.getAllByText('Unlimited (null)').length).toBeGreaterThan(0);
    const setInputCeiling = screen.getByRole('button', { name: 'Set ceiling' });
    expect(setInputCeiling).toBeEnabled();
    await userEvent.click(setInputCeiling);
    expect(screen.getByRole('slider', { name: 'Input tokens ceiling' })).toHaveValue('0');

    // When disabled via Unlimited checkbox
    await userEvent.click(screen.getByLabelText(/Unlimited duration/i));
    expect(durationInput).toHaveAttribute('placeholder', 'Unlimited');
  });
});
