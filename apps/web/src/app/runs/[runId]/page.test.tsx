import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import RunPage from './page';
import { projection } from '@/test/projection';
import { api } from '@/lib/api/client';

const mockReplace = vi.fn();
vi.mock('next/navigation', () => ({
  useParams: () => ({ runId: 'run-1' }),
  useRouter: () => ({ push: vi.fn(), replace: mockReplace }),
  useSearchParams: () => new URL(window.location.href).searchParams,
}));
vi.mock('@/hooks/use-run-events', () => ({ useRunEvents: () => 'connected' }));
vi.mock('@/lib/api/client', async original => ({ ...await original<typeof import('@/lib/api/client')>(), api: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.mocked(api).mockReset();
  mockReplace.mockClear();
  window.history.replaceState(null, '', '/');
});

test('a section deep link opens that section and section clicks update the URL', async () => {
  window.history.replaceState(null, '', '/runs/run-1?section=checks');
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path === '/runs/run-1/projection') return projection() as T;
    return { items: [], truncated: false } as T;
  });
  render(<RunPage />);
  expect(await screen.findByRole('button', { name: 'Checks' })).toHaveAttribute('aria-pressed', 'true');
  await userEvent.click(screen.getByRole('button', { name: 'Review' }));
  expect(mockReplace).toHaveBeenCalledWith('/runs/run-1?section=review', { scroll: false });
});

test('an unknown section in the URL falls back to the overview', async () => {
  window.history.replaceState(null, '', '/runs/run-1?section=bogus');
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path === '/runs/run-1/projection') return projection() as T;
    return { items: [], truncated: false } as T;
  });
  render(<RunPage />);
  expect(await screen.findByRole('button', { name: 'Overview' })).toHaveAttribute('aria-pressed', 'true');
});


test('overview explains missing worker and clears the warning after refresh recovery', async () => {
  const route = { provider: 'google', client: 'antigravity_cli', model: 'gemini-3.8-flash-medium', effort: 'medium', auth_mode: 'subscription', billing_mode: 'allowance_only' };
  let recovered = false;
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path === '/runs/run-1/projection') return projection() as T;
    if (path.startsWith('/runs/run-1/subscription-tasks')) return {
      subscription: true, tasks: [], has_more: false,
      queue_diagnostic: {
        task_id: 'task-1', purpose: 'primary', route,
        status: recovered ? 'matching_ready' : 'no_matching_registered_route',
        message: 'No current worker supports Google Antigravity.',
        guidance: 'Check worker client setup and restart the worker.',
      },
    } as T;
    return { items: [], truncated: false } as T;
  });
  render(<RunPage />);
  expect(await screen.findByRole('alert', { name: 'Queue diagnostic: matching worker unavailable' })).toHaveTextContent('Google Antigravity');
  expect(screen.getByRole('link', { name: /Inspect worker registration/ })).toHaveAttribute('href', '/subscription-profiles#client-setup');
  recovered = true;
  await userEvent.click(screen.getByRole('button', { name: 'Refresh run' }));
  await waitFor(() => expect(screen.queryByRole('alert', { name: 'Queue diagnostic: matching worker unavailable' })).not.toBeInTheDocument());
});

test('run page mounts overview and task inspector with a single subscription-task polling owner', async () => {
  const taskCalls: string[] = [];
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path === '/runs/run-1/projection') return projection() as T;
    if (path.includes('/subscription-tasks')) {
      taskCalls.push(path);
      return {
        run_id: 'run-1',
        subscription: true,
        tasks: [],
        has_more: false,
        run_allows_execution: true,
        run_is_terminal: false,
        quota_statuses: [],
      } as T;
    }
    return { items: [], truncated: false } as T;
  });

  render(<RunPage />);
  await screen.findByRole('heading', { name: 'Run usage and subscription tasks', hidden: true });

  // Exactly one request on mount shared by overview and inspector
  expect(taskCalls).toHaveLength(1);
  expect(taskCalls[0]).toBe('/runs/run-1/subscription-tasks?offset=0&limit=25');
});

test('counts actual task-endpoint requests across polling, hidden tab, refresh and pagination', async () => {
  const taskCalls: string[] = [];
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path === '/runs/run-1/projection') return projection() as T;
    if (path.includes('/subscription-tasks')) {
      taskCalls.push(path);
      return {
        run_id: 'run-1',
        subscription: true,
        tasks: [],
        has_more: true,
        run_allows_execution: true,
        run_is_terminal: false,
        quota_statuses: [],
      } as T;
    }
    return { items: [], truncated: false } as T;
  });

  render(<RunPage />);
  await screen.findByRole('heading', { name: 'Run usage and subscription tasks', hidden: true });

  // 1. Initial single request: overview and hidden TaskInspector share one request
  expect(taskCalls).toHaveLength(1);
  expect(taskCalls[0]).toBe('/runs/run-1/subscription-tasks?offset=0&limit=25');

  // 2. Explicit Refresh run from overview: shares single request while tasks tab is hidden
  await userEvent.click(screen.getByRole('button', { name: 'Refresh run' }));
  expect(taskCalls).toHaveLength(2);
  expect(taskCalls[1]).toBe('/runs/run-1/subscription-tasks?offset=0&limit=25');

  // 3. Pagination from TaskInspector: clicking Next tasks requests next page with offset=25
  await userEvent.click(screen.getByRole('button', { name: 'Next tasks', hidden: true }));
  expect(taskCalls).toHaveLength(3);
  expect(taskCalls[2]).toBe('/runs/run-1/subscription-tasks?offset=25&limit=25');

  // 4. Explicit Refresh tasks from TaskInspector: refreshes page 2
  await userEvent.click(screen.getByRole('button', { name: 'Refresh tasks', hidden: true }));
  expect(taskCalls).toHaveLength(4);
  expect(taskCalls[3]).toBe('/runs/run-1/subscription-tasks?offset=25&limit=25');
});

test('mutation refresh updates shared snapshot across overview and inspector', async () => {
  const route = { provider: 'google', client: 'antigravity_cli', model: 'gemini-3.8-flash-medium', effort: 'medium', auth_mode: 'subscription', billing_mode: 'allowance_only' };
  let status: 'no_matching_registered_route' | 'matching_ready' = 'no_matching_registered_route';

  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path === '/runs/run-1/projection') return projection() as T;
    if (path.includes('/subscription-tasks')) {
      return {
        run_id: 'run-1',
        subscription: true,
        tasks: [],
        has_more: false,
        run_allows_execution: true,
        run_is_terminal: false,
        quota_statuses: [],
        queue_diagnostic: {
          task_id: 'task-1',
          purpose: 'primary',
          route,
          status,
          message: status === 'matching_ready' ? 'Worker ready.' : 'No current worker supports Google Antigravity.',
          guidance: status === 'matching_ready' ? null : 'Check worker client setup.',
        },
      } as T;
    }
    return { items: [], truncated: false } as T;
  });

  render(<RunPage />);

  // Overview displays unready diagnostic alert from shared initial fetch
  expect(await screen.findByRole('alert', { name: 'Queue diagnostic: matching worker unavailable' })).toBeInTheDocument();

  // Backend state changes after mutation
  status = 'matching_ready';

  // Trigger refresh from TaskInspector ("Refresh tasks")
  await userEvent.click(screen.getByRole('button', { name: 'Refresh tasks', hidden: true }));

  // Shared snapshot is updated across overview: warning banner clears
  await waitFor(() => expect(screen.queryByRole('alert', { name: 'Queue diagnostic: matching worker unavailable' })).not.toBeInTheDocument());
});
