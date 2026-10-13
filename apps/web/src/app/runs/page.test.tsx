import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import RunsPage from './page';
import { api } from '@/lib/api/client';

const mockReplace = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: mockReplace }),
  usePathname: () => '/runs',
  useSearchParams: () => new URL(window.location.href).searchParams,
}));
vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));

const runItem = {
  run_id: 'run-1', task_id: 'task-1', task_title: 'Add CSV export', project_id: 'project-1', project_name: 'Forge',
  state: 'AWAITING_PLAN_APPROVAL', version: 3, pending_gate: 'plan', next_gate: 'plan', attention_required: true,
  local_remediation_count: 0, remote_remediation_count: 0, elapsed_ms: 60_000, elapsed_seconds: 60,
  created_at: '2026-10-10T09:00:00Z', updated_at: '2026-10-10T10:00:00Z',
  pull_request: null, cost_summary: { currencies: [], unpriced_calls: 0 },
};

function mockRunsApi() {
  const requested: string[] = [];
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    requested.push(path);
    if (path === '/projects') return [{ id: 'project-1', name: 'Forge' }] as T;
    if (path === '/dashboard/summary') {
      return { total_runs: 4, runs: { IMPLEMENTING: 1, AWAITING_PLAN_APPROVAL: 2, FAILED: 1 } } as T;
    }
    if (path.startsWith('/run-projections?')) return { items: [runItem], offset: 0, limit: 25, truncated: false } as T;
    throw new Error(`Unexpected API request: ${path}`);
  });
  return requested;
}

afterEach(() => {
  cleanup();
  vi.mocked(api).mockReset();
  mockReplace.mockClear();
  window.history.replaceState(null, '', '/runs');
});

test('bucket chips carry plain-language counts from the dashboard summary', async () => {
  const requested = mockRunsApi();
  render(<RunsPage />);
  expect(await screen.findByRole('button', { name: 'All runs (4)' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Waiting on you (2)' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Working (1)' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Add CSV export' })).toBeInTheDocument();
  expect(requested).toContain('/run-projections?offset=0&limit=25');
});

test('choosing a bucket writes it to the URL instead of hiding it in form state', async () => {
  mockRunsApi();
  render(<RunsPage />);
  await userEvent.click(await screen.findByRole('button', { name: 'Failed (1)' }));
  expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('bucket=failed'), expect.anything());
});

test('URL filters drive the API query: bucket states and search text', async () => {
  window.history.replaceState(null, '', '/runs?bucket=waiting&q=csv');
  const requested = mockRunsApi();
  render(<RunsPage />);
  expect(await screen.findByRole('link', { name: 'Add CSV export' })).toBeInTheDocument();
  const projections = requested.filter(path => path.startsWith('/run-projections?'));
  expect(projections.at(-1)).toContain('states=AWAITING_PLAN_APPROVAL');
  expect(projections.at(-1)).toContain('states=AWAITING_MERGE_APPROVAL');
  expect(projections.at(-1)).toContain('q=csv');
  // Counts describe all runs, so they hide while a search narrows the list.
  expect(screen.getByRole('button', { name: 'Waiting on you' })).toBeInTheDocument();
  expect(screen.getByRole('searchbox', { name: 'Search runs' })).toHaveValue('csv');
});

test('the attention filter round-trips through the URL and the API', async () => {
  window.history.replaceState(null, '', '/runs?attention=true');
  const requested = mockRunsApi();
  render(<RunsPage />);
  expect(await screen.findByRole('link', { name: 'Add CSV export' })).toBeInTheDocument();
  expect(screen.getByLabelText('Attention')).toHaveValue('true');
  const projections = requested.filter(path => path.startsWith('/run-projections?'));
  expect(projections.at(-1)).toContain('attention=true');
  // Global summary counts do not describe this narrowed set either.
  await act(async () => {});
  expect(screen.queryByRole('button', { name: /All runs \(/ })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'All runs' })).toBeInTheDocument();
});

test('an active updated range hides the all-time bucket counts', async () => {
  window.history.replaceState(null, '', '/runs?updated=7d');
  const requested = mockRunsApi();
  render(<RunsPage />);
  expect(await screen.findByRole('link', { name: 'Add CSV export' })).toBeInTheDocument();
  await act(async () => {}); // let the dashboard summary settle too
  expect(screen.queryByRole('button', { name: /All runs \(/ })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'All runs' })).toBeInTheDocument();
  const projections = requested.filter(path => path.startsWith('/run-projections?'));
  expect(projections.at(-1)).toContain('updated_since=');
});

test('refreshing re-anchors the relative window to now', async () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date('2026-10-13T12:00:00Z'));
    window.history.replaceState(null, '', '/runs?updated=24h');
    const requested = mockRunsApi();
    render(<RunsPage />);
    await act(async () => {});
    const sinceOf = (path: string) => new URLSearchParams(path.split('?')[1]).get('updated_since');
    const projections = () => requested.filter(path => path.startsWith('/run-projections?'));
    expect(sinceOf(projections().at(-1)!)).toBe('2026-10-12T12:00:00.000Z');
    vi.setSystemTime(new Date('2026-10-13T14:00:00Z'));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh runs' }));
    await act(async () => {});
    expect(sinceOf(projections().at(-1)!)).toBe('2026-10-12T14:00:00.000Z');
  } finally {
    vi.useRealTimers();
  }
});

test('refreshing runs also refreshes the bucket totals behind the chips', async () => {
  const requested = mockRunsApi();
  render(<RunsPage />);
  expect(await screen.findByRole('button', { name: 'All runs (4)' })).toBeInTheDocument();
  const summaries = () => requested.filter(path => path === '/dashboard/summary').length;
  expect(summaries()).toBe(1);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh runs' }));
  await waitFor(() => expect(summaries()).toBe(2));
});

test('search text is bounded to the API limit instead of debouncing into a 422', async () => {
  mockRunsApi();
  render(<RunsPage />);
  const input = await screen.findByRole('searchbox', { name: 'Search runs' });
  expect(input).toHaveAttribute('maxlength', '200');
  fireEvent.change(input, { target: { value: 'x'.repeat(210) } });
  await waitFor(() => expect(mockReplace).toHaveBeenCalled(), { timeout: 3000 });
  const url = String(mockReplace.mock.calls.at(-1)?.[0]);
  expect(new URLSearchParams(url.split('?')[1]).get('q')).toHaveLength(200);
});

test('typing during a slow search navigation is not reverted when it lands', async () => {
  mockRunsApi();
  mockReplace.mockImplementation((url: string) => { window.history.replaceState(null, '', url); });
  const { rerender } = render(<RunsPage />);
  const user = userEvent.setup();
  const input = await screen.findByRole('searchbox', { name: 'Search runs' });
  await user.type(input, 'foo');
  await waitFor(
    () => expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('q=foo'), expect.anything()),
    { timeout: 3000 },
  );
  await user.type(input, 'b'); // draft is now "foob"; the q=foo navigation has not committed yet
  rerender(<RunsPage />); // slow navigation commits with the older q=foo
  expect(input).toHaveValue('foob');
});

test('returning through history to a previously issued query still restores it', async () => {
  mockRunsApi();
  mockReplace.mockImplementation((url: string) => { window.history.replaceState(null, '', url); });
  const { rerender } = render(<RunsPage />);
  const user = userEvent.setup();
  const input = await screen.findByRole('searchbox', { name: 'Search runs' });
  await user.type(input, 'foo');
  await waitFor(
    () => expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('q=foo'), expect.anything()),
    { timeout: 3000 },
  );
  rerender(<RunsPage />); // q=foo landing consumes the self-issued marker
  window.history.replaceState(null, '', '/runs?q=bar');
  rerender(<RunsPage />); // external navigation to bar
  expect(input).toHaveValue('bar');
  window.history.replaceState(null, '', '/runs?q=foo');
  rerender(<RunsPage />); // history back to foo: external again, must restore
  expect(input).toHaveValue('foo');
});

test('rapid filter changes compose instead of overwriting each other before navigation commits', async () => {
  mockRunsApi(); // mockReplace never commits, so both patches race the same base
  render(<RunsPage />);
  fireEvent.change(await screen.findByLabelText('Project'), { target: { value: 'project-1' } });
  fireEvent.click(screen.getByRole('button', { name: /Failed/ }));
  const url = String(mockReplace.mock.calls.at(-1)?.[0]);
  const params = new URLSearchParams(url.split('?')[1]);
  expect(params.get('project_id')).toBe('project-1');
  expect(params.get('bucket')).toBe('failed');
});

test('typing in search updates the URL query after a short debounce', async () => {
  mockRunsApi();
  render(<RunsPage />);
  const user = userEvent.setup();
  await user.type(await screen.findByRole('searchbox', { name: 'Search runs' }), 'csv');
  await waitFor(
    () => expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('q=csv'), expect.anything()),
    { timeout: 3000 },
  );
});
