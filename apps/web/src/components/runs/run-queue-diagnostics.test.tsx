import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { RunQueueDiagnosticsBanner } from './run-queue-diagnostics';
import { RunCockpit } from './run-cockpit';
import { projection } from '@/test/projection';
import { api } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';

vi.mock('@/lib/api/client', async original => ({ ...await original<typeof import('@/lib/api/client')>(), api: vi.fn() }));

afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });

type Diagnostic = components['schemas']['SubscriptionQueueDiagnosticView'];

const googleRoute: components['schemas']['AttemptRoute'] = {
  provider: 'google',
  client: 'antigravity_cli',
  model: 'gemini-3.8-flash-medium',
  effort: 'medium',
  auth_mode: 'subscription',
  billing_mode: 'allowance_only',
};

test('renders prominent alert on no_current_worker with link to worker setup', () => {
  const diag: Diagnostic = {
    task_id: 'task-1',
    purpose: 'primary',
    status: 'no_current_worker',
    route: googleRoute,
    message: 'Queued primary task requires google / antigravity_cli / gemini-3.8-flash-medium (medium effort), but no Forge worker is currently reporting.',
    guidance: 'Start the Forge worker to begin processing queued tasks. If you configured clients in .env or an installations manifest, check that the worker process is loading the intended manifest (for example, verify FORGE_SUBSCRIPTION_INSTALLATIONS_PATH if set in the worker\'s environment).',
    warnings: ['approved_tools_unproved'],
    available_routes: [],
  };

  render(<RunQueueDiagnosticsBanner diagnostic={diag} />);

  const alert = screen.getByRole('alert', { name: 'Queue diagnostic: matching worker unavailable' });
  expect(alert).toHaveTextContent('Queued task cannot start: no worker reporting');
  expect(alert).toHaveTextContent('google / antigravity_cli / gemini-3.8-flash-medium (medium effort)');
  expect(alert).toHaveTextContent('no Forge worker is currently reporting');
  expect(alert).toHaveTextContent('FORGE_SUBSCRIPTION_INSTALLATIONS_PATH');
  expect(alert).toHaveTextContent('Forge cannot guarantee that Antigravity limits itself to Forge-approved tools');

  const link = screen.getByRole('link', { name: /Inspect worker registration and client setup/ });
  expect(link).toHaveAttribute('href', '/subscription-profiles#client-setup');
});

test('renders prominent alert on no_matching_registered_route with current worker registered routes', () => {
  const diag: Diagnostic = {
    task_id: 'task-1',
    purpose: 'primary',
    status: 'no_matching_registered_route',
    route: googleRoute,
    message: 'Queued primary task requires google / antigravity_cli / gemini-3.8-flash-medium (medium effort), but no current worker has registered this route.',
    guidance: 'Current worker(s) registered: openai / codex_app_server / gpt-5 · muse / muse_client / muse-chat. Check your worker configuration and client setup.',
    warnings: ['approved_tools_unproved'],
    available_routes: [
      { provider: 'openai', client: 'codex_app_server', model: 'gpt-5', effort: 'none', auth_mode: 'subscription', billing_mode: 'allowance_only' },
      { provider: 'muse', client: 'muse_client', model: 'muse-chat', effort: 'none', auth_mode: 'subscription', billing_mode: 'allowance_only' },
    ],
  };

  render(<RunQueueDiagnosticsBanner diagnostic={diag} />);

  const alert = screen.getByRole('alert');
  expect(alert).toHaveTextContent('Queued task cannot start: no matching worker registered');
  expect(alert).toHaveTextContent('no current worker has registered this route');
  expect(alert).toHaveTextContent('codex_app_server');
  expect(alert).toHaveTextContent('muse_client');
});

test('renders prominent alert on matched_unavailable with reason and guidance', () => {
  const diag: Diagnostic = {
    task_id: 'task-1',
    purpose: 'primary',
    status: 'matched_unavailable',
    route: googleRoute,
    reason: 'signed_out',
    message: 'Queued primary task requires google / antigravity_cli / gemini-3.8-flash-medium (medium effort). A current worker registered this route, but it is currently unavailable: client is signed out.',
    guidance: 'Sign in with Google inside the official Antigravity client, then refresh.',
    warnings: ['approved_tools_unproved'],
    available_routes: [googleRoute],
  };

  render(<RunQueueDiagnosticsBanner diagnostic={diag} />);

  const alert = screen.getByRole('alert');
  expect(alert).toHaveTextContent('Queued task cannot start: matching worker unavailable');
  expect(alert).toHaveTextContent('client is signed out');
  expect(alert).toHaveTextContent('Sign in with Google');
});

test('matching_ready produces no warning alert', () => {
  const diag: Diagnostic = {
    task_id: 'task-1',
    purpose: 'primary',
    status: 'matching_ready',
    route: googleRoute,
    message: 'A current worker has registered google / antigravity_cli / gemini-3.8-flash-medium (medium effort) and is ready to attempt.',
    guidance: 'Worker registration is satisfied.',
    warnings: ['approved_tools_unproved', 'operator_trusted'],
    available_routes: [googleRoute],
  };

  const { container } = render(<RunQueueDiagnosticsBanner diagnostic={diag} />);
  expect(container.firstChild).toBeNull();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

test('RunCockpit displays queue diagnostics on default overview and clears on tab change', async () => {
  const diag: Diagnostic = {
    task_id: 'task-1',
    purpose: 'primary',
    status: 'no_matching_registered_route',
    route: googleRoute,
    message: 'Queued primary task requires google / antigravity_cli / gemini-3.8-flash-medium (medium effort), but no current worker has registered this route.',
    guidance: 'Current worker(s) registered: openai / codex_app_server / gpt-5.',
    warnings: ['approved_tools_unproved'],
    available_routes: [],
  };

  const onTasks = vi.fn();
  const banner = <RunQueueDiagnosticsBanner diagnostic={diag} onTasks={onTasks} />;

  render(<RunCockpit initial={projection()} queueDiagnostics={banner} />);

  // Visible on default overview
  expect(screen.getByRole('alert', { name: 'Queue diagnostic: matching worker unavailable' })).toBeInTheDocument();

  // Clicking inspect tasks triggers onTasks callback
  await userEvent.click(screen.getByRole('button', { name: 'Inspect tasks' }));
  expect(onTasks).toHaveBeenCalled();
});

test('Refresh run invokes onRefreshTasks to recover queue state', async () => {
  const initial = projection();
  vi.mocked(api).mockResolvedValue(initial);
  const onRefreshTasks = vi.fn();
  render(<RunCockpit initial={initial} onRefreshTasks={onRefreshTasks} />);

  await userEvent.click(screen.getByRole('button', { name: 'Refresh run' }));
  expect(onRefreshTasks).toHaveBeenCalled();
});
