import { useEffect, useState } from 'react';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { RunCockpit } from './run-cockpit';
import { projection } from '@/test/projection';
import { api } from '@/lib/api/client';

const transport = vi.hoisted(() => ({ connection: 'connected' }));
vi.mock('@/hooks/use-run-events', () => ({ useRunEvents: () => transport.connection }));
vi.mock('@/lib/api/client', async original => ({ ...await original<typeof import('@/lib/api/client')>(), api: vi.fn() }));
beforeEach(() => { transport.connection = 'connected'; vi.mocked(api).mockResolvedValue({ items: [], truncated: false }); });
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });

test('status, progress and overview use distinct accessible regions', () => {
  render(<RunCockpit initial={projection()} />);
  expect(screen.getByRole('region', { name: 'Current run status' })).toHaveTextContent('Plan approval needed');
  expect(screen.getByRole('region', { name: 'Workflow progress' })).toBeInTheDocument();
  expect(screen.getByRole('region', { name: 'Checks at a glance' })).toBeInTheDocument();
  expect(screen.getByRole('complementary', { name: 'Run context' })).toBeInTheDocument();
});

test('overview shortcuts transfer keyboard focus to the persistent section button', async () => {
  render(<RunCockpit initial={projection()} />);
  await userEvent.click(screen.getByRole('button', { name: 'View checks' }));
  expect(screen.getByRole('button', { name: 'Checks' })).toHaveFocus();
  expect(screen.getByRole('region', { name: 'Check evidence' })).toBeInTheDocument();
});

test('the task inspector remains mounted and retains input across section changes', async () => {
  const mounted = vi.fn();
  function Inspector() {
    const [reason, setReason] = useState('');
    useEffect(() => { mounted(); }, []);
    return <label>Pending task reason<input value={reason} onChange={event => setReason(event.target.value)} /></label>;
  }
  render(<RunCockpit initial={projection()} tasks={<Inspector />} />);
  expect(screen.queryByRole('textbox', { name: 'Pending task reason' })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Tasks' }));
  await userEvent.type(screen.getByRole('textbox', { name: 'Pending task reason' }), 'Retain this request');
  await userEvent.click(screen.getByRole('button', { name: 'Overview' }));
  await userEvent.click(screen.getByRole('button', { name: 'Tasks' }));
  expect(screen.getByRole('textbox', { name: 'Pending task reason' })).toHaveValue('Retain this request');
  expect(mounted).toHaveBeenCalledTimes(1);
});

test('the subscription-tasks deep link opens the Tasks section', async () => {
  window.history.replaceState({}, '', '/runs/run-1#subscription-tasks');
  render(<RunCockpit initial={projection()} tasks={<span>Task inspector</span>} />);
  expect(await screen.findByText('Task inspector')).toBeVisible();
  expect(screen.getByRole('button', { name: 'Tasks' })).toHaveAttribute('aria-pressed', 'true');
});

test('connection loss leaves authorised actions disabled and last-known state explicit', () => {
  transport.connection = 'reconnecting';
  render(<RunCockpit initial={projection()} />);
  expect(screen.getByRole('button', { name: 'Approve plan' })).toBeDisabled();
  expect(screen.getByRole('region', { name: 'Current run status' })).toHaveTextContent('Last known state');
  expect(screen.getByRole('status')).toHaveTextContent('Actions stay disabled');
});

test('a success recorded on another head is never shown as a current pass', () => {
  const p = projection(); p.candidate.commit = 'b'.repeat(40);
  p.checks = [{ id: 'check-1', name: 'Typecheck', command_name: 'typecheck', command_version: 1,
    status: 'SUCCEEDED', completed_at: null, exit_code: 0, head_sha: 'a'.repeat(40), output_artifact_digest: null }];
  render(<RunCockpit initial={p} />);
  const summary = within(screen.getByRole('region', { name: 'Checks at a glance' }));
  expect(summary.getByText('Different head')).toBeInTheDocument();
  expect(summary.queryByText('Passed', { exact: true })).not.toBeInTheDocument();
});

test('stale check results are announced as last known, not current passes', () => {
  transport.connection = 'reconnecting';
  const p = projection(); p.checks = [{ id: 'check-1', name: 'Typecheck', command_name: 'typecheck', command_version: 1,
    status: 'SUCCEEDED', completed_at: null, exit_code: 0, head_sha: 'a'.repeat(40), output_artifact_digest: null }];
  p.candidate.commit = 'a'.repeat(40);
  render(<RunCockpit initial={p} />);
  const summary = within(screen.getByRole('region', { name: 'Checks at a glance' }));
  expect(summary.getByText('Last known: Passed')).toBeInTheDocument();
  expect(summary.queryByText('Passed', { exact: true })).not.toBeInTheDocument();
});

test('unsandboxed execution stays visible outside the context disclosure', () => {
  const p = projection(); p.security.runner_mode = 'trusted_host';
  render(<RunCockpit initial={p} />);
  expect(screen.getByText(/Commands are not isolated in Docker/)).toBeVisible();
});

test('the visible status carries the exact authoritative state after a refresh', async () => {
  const initial = projection();
  const next = projection(); next.run.state = 'IMPLEMENTING'; next.run.version = initial.run.version + 1;
  vi.mocked(api).mockResolvedValue(next);
  render(<RunCockpit initial={initial} />);
  expect(screen.getByRole('region', { name: 'Current run status' })).toHaveAttribute('data-run-state', 'AWAITING_PLAN_APPROVAL');
  await userEvent.click(screen.getByRole('button', { name: 'Refresh run' }));
  expect(await screen.findByRole('heading', { name: 'Implementation phase' })).toBeInTheDocument();
  expect(screen.getByRole('region', { name: 'Current run status' })).toHaveAttribute('data-run-state', 'IMPLEMENTING');
});

test('rerendering with a new tasks element does not retrigger deep link selection after switching to overview', async () => {
  window.history.replaceState({}, '', '/runs/run-1#subscription-tasks');
  const { rerender } = render(<RunCockpit initial={projection()} tasks={<span>Task inspector initial</span>} />);
  expect(await screen.findByText('Task inspector initial')).toBeVisible();
  expect(screen.getByRole('button', { name: 'Tasks' })).toHaveAttribute('aria-pressed', 'true');

  await userEvent.click(screen.getByRole('button', { name: 'Overview' }));
  expect(screen.getByRole('button', { name: 'Overview' })).toHaveAttribute('aria-pressed', 'true');

  rerender(<RunCockpit initial={projection()} tasks={<span>Task inspector updated</span>} />);
  expect(screen.getByRole('button', { name: 'Overview' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByRole('button', { name: 'Tasks' })).toHaveAttribute('aria-pressed', 'false');
});

test('status heading and authority disclosure distinguish remote repair from local remediation', () => {
  const remoteP = projection();
  remoteP.run.state = 'REMEDIATING';
  remoteP.budgets.remote_remediation_remaining = 2;
  remoteP.latest_events = [
    {
      sequence: 1,
      run_version: 4,
      actor_class: 'worker',
      event_type: 'pr.observation_recorded',
      occurred_at: '2026-01-01T00:00:00Z',
      payload: { disposition: 'remediate', observation_digest: 'c'.repeat(64) },
    },
    {
      sequence: 2,
      run_version: 4,
      actor_class: 'worker',
      event_type: 'run.state_changed',
      occurred_at: '2026-01-01T00:00:01Z',
      payload: { target: 'REMEDIATING' },
    },
  ];

  const { unmount } = render(<RunCockpit initial={remoteP} />);
  expect(within(screen.getByRole('region', { name: 'Current run status' })).getByRole('heading', { name: 'Repairing observed PR findings' })).toBeInTheDocument();
  expect(screen.getByRole('region', { name: 'Current autonomy' })).toHaveTextContent(
    'Forge may repair the pull request with 2 remote repair attempts remaining. Publishing or merging still requires exact human approval.',
  );
  unmount();

  const localP = projection();
  localP.run.state = 'REMEDIATING';
  localP.budgets.local_remediation_remaining = 1;
  localP.latest_events = [
    {
      sequence: 3,
      run_version: 5,
      actor_class: 'worker',
      event_type: 'run.review_decided',
      occurred_at: '2026-01-01T00:00:02Z',
      payload: { target: 'REMEDIATING' },
    },
  ];

  render(<RunCockpit initial={localP} />);
  expect(within(screen.getByRole('region', { name: 'Current run status' })).getByRole('heading', { name: 'Repairing local findings' })).toBeInTheDocument();
  expect(screen.getByRole('region', { name: 'Current autonomy' })).toHaveTextContent(
    'Forge may repair local findings within the remaining 1 remediation attempts.',
  );
});

