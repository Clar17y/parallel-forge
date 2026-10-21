import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';
import { projection } from '@/test/projection';
import { AutonomyBanner } from './autonomy-banner';

afterEach(cleanup);

test.each(['CREATED', 'PLANNING'] as const)('ordinary %s authority covers internal preparation and local work', state => {
  const p = projection();
  p.run.state = state;
  p.run.workflow = { review_plan_first: false, delegation: 'auto', optional_review: 'auto' };
  render(<AutonomyBanner projection={p} />);
  const authority = screen.getByRole('region', { name: 'Current autonomy' });
  expect(authority).toHaveTextContent('prepare internally');
  expect(authority).toHaveTextContent('Publishing a pull request requires separate approval');
  expect(authority).not.toHaveTextContent('Implementation requires plan approval');
});

test.each([null, { review_plan_first: true, delegation: 'auto', optional_review: 'auto' }] as const)('legacy and plan-first authority retains the explicit gate (%j)', workflow => {
  const p = projection();
  p.run.state = 'PLANNING';
  p.run.workflow = workflow;
  render(<AutonomyBanner projection={p} />);
  expect(screen.getByRole('region', { name: 'Current autonomy' })).toHaveTextContent('Implementation requires plan approval');
});

test('ordinary pending-plan authority asks for input without treating an answer as approval', () => {
  const p = projection();
  p.run.state = 'AWAITING_PLAN_APPROVAL';
  p.run.workflow = { review_plan_first: false, delegation: 'auto', optional_review: 'auto' };
  p.next_gate = 'plan';
  render(<AutonomyBanner projection={p} />);
  expect(screen.getByRole('region', { name: 'Current autonomy' })).toHaveTextContent('waiting for owner input');
});

test('implementation authority describes recorded preparation independently of current preferences', () => {
  const p = projection();
  p.run.state = 'IMPLEMENTING';
  p.run.workflow = { review_plan_first: true, delegation: 'auto', optional_review: 'auto' };
  p.plan.authority = 'internal_preparation';
  render(<AutonomyBanner projection={p} />);
  expect(screen.getByRole('region', { name: 'Current autonomy' })).toHaveTextContent('internally prepared scope');
});
