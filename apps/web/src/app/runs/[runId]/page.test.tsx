import { cleanup, render, screen } from '@testing-library/react';
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
vi.mock('@/components/subscription-tasks/task-inspector', () => ({ TaskInspector: () => null }));
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
