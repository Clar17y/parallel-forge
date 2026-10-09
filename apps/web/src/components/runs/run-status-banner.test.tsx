import { render, screen } from '@testing-library/react';
import { expect, test } from 'vitest';
import { projection } from '@/test/projection';
import { RunStatusBanner } from './run-status-banner';

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
