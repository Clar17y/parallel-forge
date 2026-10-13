import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { AppShell } from './app-shell';
import { ThemeProvider } from '@/components/theme/theme-provider';
import { api } from '@/lib/api/client';
vi.mock('next/navigation', () => ({ usePathname: () => '/runs/example' }));
vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });

function mockPendingApprovals(count: number, truncated = false) {
  vi.mocked(api).mockImplementation(async <T,>(path: string) => {
    if (path.startsWith('/approvals?')) return { items: Array.from({ length: count }, (_, index) => ({ run_id: `run-${index}` })), offset: 0, limit: 25, truncated } as T;
    throw new Error(`Unexpected API request: ${path}`);
  });
}

test('shell exposes grouped navigation, real links and a skip target', () => {
  mockPendingApprovals(0);
  render(<ThemeProvider><AppShell><h1>Example run</h1></AppShell></ThemeProvider>);
  const sidebar = within(screen.getByRole('complementary', { name: 'Sidebar' }));
  expect(sidebar.getByRole('region', { name: 'Operate' })).toBeInTheDocument();
  expect(sidebar.getByRole('region', { name: 'Govern' })).toBeInTheDocument();
  expect(sidebar.getByRole('region', { name: 'Inspect' })).toBeInTheDocument();
  expect(sidebar.getByRole('link', { name: 'Runs' })).toHaveAttribute('aria-current', 'page');
  expect(screen.getByRole('link', { name: 'Forge home' })).toHaveAttribute('href', '/');
  expect(screen.getByRole('link', { name: 'New run' })).toHaveAttribute('href', '/runs/new');
  expect(screen.getByRole('link', { name: 'Skip to content' })).toHaveAttribute('href', '#main-content');
  expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content');
  expect(screen.getByRole('combobox', { name: 'Appearance' })).toHaveValue('system');
  expect(screen.queryByText('Session active')).not.toBeInTheDocument();
});

test('shell badges the approvals link with the live pending count', async () => {
  mockPendingApprovals(2);
  render(<ThemeProvider><AppShell><h1>Example run</h1></AppShell></ThemeProvider>);
  const sidebar = within(screen.getByRole('complementary', { name: 'Sidebar' }));
  expect(await sidebar.findByRole('link', { name: /Approvals 2/ })).toHaveAttribute('href', '/approvals');
});
