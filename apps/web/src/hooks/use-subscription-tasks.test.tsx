import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { api } from '@/lib/api/client';
import {
  useSubscriptionTasks,
  SubscriptionTasksProvider,
  useSubscriptionTasksContext,
  type SubscriptionTaskPage,
} from './use-subscription-tasks';
import { TaskInspector } from '@/components/subscription-tasks/task-inspector';

vi.mock('@/lib/api/client', async original => ({
  ...await original<typeof import('@/lib/api/client')>(),
  api: vi.fn(),
}));

function makeTaskPage(overrides: Partial<SubscriptionTaskPage> = {}): SubscriptionTaskPage {
  return {
    run_id: 'run-1',
    subscription: true,
    candidate_epoch: 1,
    candidate_state: 'open',
    has_more: false,
    tasks: [],
    run_allows_execution: true,
    run_is_terminal: false,
    run_version: 1,
    recovery_attention: false,
    ...overrides,
  };
}

describe('useSubscriptionTasks hook', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  test('initial single request and one settled poll per interval', async () => {
    const requestedPaths: string[] = [];
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      requestedPaths.push(path);
      return makeTaskPage() as T;
    });

    const { result, unmount } = renderHook(() => useSubscriptionTasks('run-1', { refreshIntervalMs: 5000 }));

    expect(requestedPaths).toEqual(['/runs/run-1/subscription-tasks?offset=0&limit=25']);

    // Let the initial request settle
    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.tasks.value?.run_id).toBe('run-1');
    expect(requestedPaths).toHaveLength(1);

    // Advance by 5000ms: triggers next poll
    await act(async () => {
      vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });

    expect(requestedPaths).toHaveLength(2);
    expect(requestedPaths[1]).toBe('/runs/run-1/subscription-tasks?offset=0&limit=25');

    // Advance by another 5000ms: exactly one settled poll per interval
    await act(async () => {
      vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });

    expect(requestedPaths).toHaveLength(3);

    // Unmount cancels polling timer
    unmount();
    await act(async () => {
      vi.advanceTimersByTime(10000);
      await Promise.resolve();
    });

    expect(requestedPaths).toHaveLength(3);
  });

  test('refresh remains stable when the shared snapshot or selection changes', async () => {
    vi.mocked(api).mockImplementation(async <T,>() => makeTaskPage() as T);
    const { result } = renderHook(() => useSubscriptionTasks('run-1'));
    const refresh = result.current.refresh;
    await act(async () => { await Promise.resolve(); });
    act(() => result.current.setSelected('task-1'));
    expect(result.current.refresh).toBe(refresh);
  });

  test('unmount aborts an in-flight shared request and prevents further polling', async () => {
    const signals: AbortSignal[] = [];
    vi.mocked(api).mockImplementation(<T,>(_path: string, options?: RequestInit) => {
      if (options?.signal) signals.push(options.signal);
      return new Promise<T>(() => {});
    });
    const { unmount } = renderHook(() => useSubscriptionTasks('run-1'));
    expect(signals).toHaveLength(1);
    expect(signals[0].aborted).toBe(false);
    unmount();
    expect(signals[0].aborted).toBe(true);
    await act(async () => { vi.advanceTimersByTime(10000); });
    expect(signals).toHaveLength(1);
  });

  test('pagination updates offset, clears selection, and requests the new page', async () => {
    const requestedPaths: string[] = [];
    vi.mocked(api).mockImplementation(async <T,>(path: string) => {
      requestedPaths.push(path);
      return makeTaskPage({ has_more: true }) as T;
    });

    const { result } = renderHook(() => useSubscriptionTasks('run-1'));

    await act(async () => {
      await Promise.resolve();
    });

    // Select a task
    act(() => {
      result.current.setSelected('task-1');
    });
    expect(result.current.selected).toBe('task-1');

    // Move to next page
    act(() => {
      result.current.movePage(25);
    });

    expect(result.current.offset).toBe(25);
    expect(result.current.selected).toBeNull();

    await act(async () => {
      await Promise.resolve();
    });

    expect(requestedPaths).toContain('/runs/run-1/subscription-tasks?offset=25&limit=25');
  });

  test('run-id reset: changing runId resets offset to 0 and clears selection', async () => {
    vi.mocked(api).mockImplementation(async <T,>() => makeTaskPage() as T);

    let currentRunId = 'run-1';
    const { result, rerender } = renderHook(() => useSubscriptionTasks(currentRunId));

    await act(async () => {
      await Promise.resolve();
    });

    act(() => {
      result.current.movePage(50);
      result.current.setSelected('task-42');
    });

    expect(result.current.offset).toBe(50);
    expect(result.current.selected).toBe('task-42');

    // Switch runId
    currentRunId = 'run-2';
    rerender();

    expect(result.current.offset).toBe(0);
    expect(result.current.selected).toBeNull();
    expect(result.current.runId).toBe('run-2');
  });

  test('explicit refresh returns token and maintains token fencing', async () => {
    let callCount = 0;
    vi.mocked(api).mockImplementation(async <T,>() => {
      callCount++;
      return makeTaskPage({ run_version: callCount }) as T;
    });

    const { result } = renderHook(() => useSubscriptionTasks('run-1'));

    await act(async () => {
      await Promise.resolve();
    });

    const initialToken = result.current.tasks.token;

    let returnedToken = 0;
    act(() => {
      returnedToken = result.current.refresh();
    });

    // returnedToken is immediately incremented for fencing
    expect(returnedToken).toBeGreaterThan(initialToken);
    expect(result.current.tasks.refreshing).toBe(true);

    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.tasks.refreshing).toBe(false);
    expect(result.current.tasks.token).toBe(returnedToken);
    expect(result.current.tasks.value?.run_version).toBe(2);
  });

  test('context provider exposes shared controller to nested consumers', async () => {
    vi.mocked(api).mockImplementation(async <T,>() => makeTaskPage() as T);

    function Consumer() {
      const state = useSubscriptionTasksContext();
      if (!state) return <div>No context</div>;
      return <div>Run: {state.runId}, Offset: {state.offset}</div>;
    }

    function App() {
      const controller = useSubscriptionTasks('run-100');
      return (
        <SubscriptionTasksProvider value={controller}>
          <Consumer />
        </SubscriptionTasksProvider>
      );
    }

    render(<App />);
    expect(screen.getByText('Run: run-100, Offset: 0')).toBeInTheDocument();
  });
});
