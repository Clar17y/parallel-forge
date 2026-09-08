import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { RunCockpit } from './run-cockpit';
import { projection } from '@/test/projection';
import { api } from '@/lib/api/client';

vi.mock('@/hooks/use-run-events', () => ({ useRunEvents: () => 'connected' }));
vi.mock('@/lib/api/client', async original => ({ ...await original<typeof import('@/lib/api/client')>(), api: vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });
test('a mismatched refreshed projection disables actions and reports unavailable state', async () => {
  const wrong = projection(); wrong.run.id = 'another-run';
  vi.mocked(api).mockResolvedValue(wrong);
  render(<RunCockpit initial={projection()} />);
  await userEvent.click(screen.getByRole('button', { name: 'Refresh run' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('could not be refreshed');
  expect(screen.getByRole('button', { name: 'Approve plan' })).toBeDisabled();
});
test('a disabled database is not a missing resource and terminal runs have no inferred controls', () => {
  const initial = projection();
  initial.run.state = 'COMPLETED'; initial.available_commands = [];
  render(<RunCockpit initial={initial} />);
  expect(screen.getByText('Not configured')).toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Approve plan' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Cancel run' })).not.toBeInTheDocument();
});
