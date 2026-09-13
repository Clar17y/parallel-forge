import { render, screen } from '@testing-library/react';
import { expect, test } from 'vitest';
import { QuotaStatusList } from './quota-status';

test('shows known reset and probe labels without fabricating balances', () => {
  render(<QuotaStatusList statuses={[{
    provider: 'openai', account: 'team-a', pool: 'allowance', status: 'blocked',
    reason: 'operator_report', observed_at: '2026-09-12T12:00:00Z',
    reset_at: '2026-09-12T13:00:00Z', next_eligible_at: null,
    retry_basis: 'known_reset', probe_attempt_id: null, revision: 1, recovered_at: null,
  }]} />);
  expect(screen.getByText(/Known reset:/)).toBeInTheDocument();
  expect(screen.queryByText(/balance|tokens|credits/i)).not.toBeInTheDocument();
});

test('labels unknown next probe and in-flight probe', () => {
  render(<QuotaStatusList statuses={[{
    provider: 'anthropic', account: 'team-b', pool: 'allowance', status: 'unknown',
    reason: 'probe_pending', observed_at: '2026-09-12T12:00:00Z',
    reset_at: null, next_eligible_at: '2026-09-12T13:00:00Z',
    retry_basis: 'probe_cooldown', probe_attempt_id: 'attempt-1', revision: 1, recovered_at: null,
  }]} />);
  expect(screen.getByText(/Next probe:/)).toBeInTheDocument();
  expect(screen.getByText(/Probe in flight: attempt-1/)).toBeInTheDocument();
});
