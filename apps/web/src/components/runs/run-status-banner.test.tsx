import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';
import { projection } from '@/test/projection';
import { RunStatusBanner } from './run-status-banner';

import { describeRunState, nextGateMessage } from './run-presentation';

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

test('presents AWAITING_PR_APPROVAL as Local result ready for ordinary workflow runs', () => {
  const value = projection();
  value.run.state = 'AWAITING_PR_APPROVAL';
  value.run.workflow = {
    review_plan_first: false,
    delegation: 'auto',
    optional_review: 'auto',
  };
  const desc = describeRunState(value);
  expect(desc.title).toBe('Local result ready');
  expect(desc.tone).toBe('success');
  expect(desc.description).toContain('PR publication is an explicit owner action');

  // Reviewing the plan first does not make the tested local result incomplete.
  value.run.workflow.review_plan_first = true;
  expect(describeRunState(value).title).toBe('Local result ready');
  value.run.workflow = null;
  expect(describeRunState(value).title).toBe('PR publication approval needed');
});

test('ordinary pending-plan attention describes owner input rather than another compulsory approval', () => {
  const value = projection();
  value.run.state = 'AWAITING_PLAN_APPROVAL';
  value.run.workflow = { review_plan_first: false, delegation: 'auto', optional_review: 'auto' };
  value.next_gate = 'plan';
  expect(describeRunState(value).title).toBe('Preparation needs owner input');
  expect(nextGateMessage(value)).toContain('Owner input');
  value.run.workflow.review_plan_first = true;
  expect(describeRunState(value).title).toBe('Plan approval needed');
  expect(nextGateMessage(value)).toContain('Plan approval');
});

test('presents CREATED and PLANNING states as internal preparation for ordinary workflow runs', () => {
  const value = projection();
  value.run.workflow = {
    review_plan_first: false,
    delegation: 'auto',
    optional_review: 'auto',
  };

  value.run.state = 'CREATED';
  const createdDesc = describeRunState(value);
  expect(createdDesc.title).toBe('Run created');
  expect(createdDesc.description).toContain('internal preparation');
  expect(createdDesc.description).not.toContain('separate plan approval');

  value.run.state = 'PLANNING';
  const planningDesc = describeRunState(value);
  expect(planningDesc.title).toBe('Preparing a plan');
  expect(planningDesc.description).toContain('internal preparation');
  expect(planningDesc.description).not.toContain('Review the plan before authorising implementation');
});

test('preserves legacy and opt-in review plan first status copy', () => {
  const value = projection();
  value.run.workflow = null; // legacy

  value.run.state = 'CREATED';
  expect(describeRunState(value).description).toBe('Planning is next. Implementation requires a separate plan approval.');

  value.run.state = 'PLANNING';
  expect(describeRunState(value).description).toBe('Forge is in the planning phase. Review the plan before authorising implementation.');

  // Opt-in review_plan_first: true
  value.run.workflow = {
    review_plan_first: true,
    delegation: 'auto',
    optional_review: 'auto',
  };
  value.run.state = 'CREATED';
  expect(describeRunState(value).description).toBe('Planning is next. Implementation requires a separate plan approval.');

  value.run.state = 'PLANNING';
  expect(describeRunState(value).description).toBe('Forge is in the planning phase. Review the plan before authorising implementation.');
});

test('presents next-gate message as internal preparation during CREATED and PLANNING for ordinary runs', () => {
  const value = projection();
  value.run.workflow = {
    review_plan_first: false,
    delegation: 'auto',
    optional_review: 'auto',
  };

  value.run.state = 'CREATED';
  value.next_gate = 'pr';
  expect(nextGateMessage(value)).toContain('Internal preparation in progress');

  value.run.state = 'PLANNING';
  expect(nextGateMessage(value)).toContain('Internal preparation in progress');

  // Opt-in review_plan_first waits for plan approval
  value.run.workflow.review_plan_first = true;
  value.next_gate = 'plan';
  expect(nextGateMessage(value)).toContain('Plan approval');
});
