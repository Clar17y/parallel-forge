import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { AppShell } from './app-shell';
vi.mock('next/navigation', () => ({ usePathname: () => '/runs/example' }));
afterEach(cleanup);

test('shell exposes grouped navigation, real links and a skip target', () => {
  render(<AppShell><h1>Example run</h1></AppShell>);
  const sidebar = within(screen.getByRole('complementary', { name: 'Sidebar' }));
  expect(sidebar.getByRole('region', { name: 'Operate' })).toBeInTheDocument();
  expect(sidebar.getByRole('region', { name: 'Govern' })).toBeInTheDocument();
  expect(sidebar.getByRole('region', { name: 'Inspect' })).toBeInTheDocument();
  expect(sidebar.getByRole('link', { name: 'Runs' })).toHaveAttribute('aria-current', 'page');
  expect(screen.getByRole('link', { name: 'New run' })).toHaveAttribute('href', '/runs/new');
  expect(screen.getByRole('link', { name: 'Skip to content' })).toHaveAttribute('href', '#main-content');
  expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content');
  expect(screen.queryByText('Session active')).not.toBeInTheDocument();
});
