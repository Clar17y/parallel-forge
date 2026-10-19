import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';
import { projection } from '@/test/projection';
import { RunStatusBanner } from './run-status-banner';

import { describeRunState } from './run-presentation';

afterEach(cleanup);

test('workflow phase activity stops for approvals, terminal states, recovery and stale reads', () => {
  const value = projection();
  value.run.state = 'IMPLEMENTING';
  const view = render(<RunStatusBanner projection={value} stale={false} />);
  expect(screen.getByLabelText('Current run status').querySelector('svg.animate-spin')).toBeInTheDocument();
  expect(screen.getByText('Implementation phase')).toBeInTheDocument();

  for (const state of ['AWAITING_PLAN_APPROVAL', 'COMPLETED', 'FAILED', 'PAUSED'] as const) {
    value.run.state = state;
    view.rerender(<RunStatusBanner projection={value} stale={false} />);
    expect(screen.getByLabelText('Current run status').querySelector('svg.animate-spin')).not.toBeInTheDocument();
  }
  value.run.state = 'IMPLEMENTING';
  value.recovery_hold = true;
  view.rerender(<RunStatusBanner projection={value} stale={false} />);
  expect(screen.getByLabelText('Current run status').querySelector('svg.animate-spin')).not.toBeInTheDocument();
  value.recovery_hold = false;
  view.rerender(<RunStatusBanner projection={value} stale={true} />);
  expect(screen.getByLabelText('Current run status').querySelector('svg.animate-spin')).not.toBeInTheDocument();
  expect(screen.getByText(/Last known state:/)).toBeInTheDocument();
});

test('does not show planning spinner when attempt has ended and run is waiting', () => {
  const value = projection();
  value.run.state = 'PLANNING';
  value.stall_reason = 'The planning attempt ended and nothing is running.';
  render(<RunStatusBanner projection={value} stale={false} />);
  expect(screen.getByLabelText('Current run status').querySelector('svg.animate-spin')).not.toBeInTheDocument();
  expect(screen.getByText('Run waiting')).toBeInTheDocument();
});

test('retained stall reason does not relabel terminal, paused, or approval/attention states as waiting', () => {
  const value = projection({ stall_reason: 'The planning attempt ended and nothing is running.' });
  const terminalOrSpecialStates = [
    ['COMPLETED', 'Run completed'],
    ['FAILED', 'Run failed'],
    ['CANCELLED', 'Run cancelled'],
    ['PAUSED', 'Run paused'],
    ['AWAITING_PLAN_APPROVAL', 'Plan approval needed'],
    ['AWAITING_PR_APPROVAL', 'PR publication approval needed'],
    ['AWAITING_MERGE_APPROVAL', 'Merge approval needed'],
    ['AWAITING_HUMAN_INTERVENTION', 'Human attention required'],
  ] as const;

  for (const [state, expectedTitle] of terminalOrSpecialStates) {
    value.run.state = state;
    expect(describeRunState(value).title).toBe(expectedTitle);
  }
});
