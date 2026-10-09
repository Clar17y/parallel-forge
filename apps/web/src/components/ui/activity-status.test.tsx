import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, test } from 'vitest';
import { ActivityStatus, ActivitySpinner } from './activity-status';

describe('ActivityStatus', () => {
  afterEach(cleanup);

  test('executing state renders active spinner and region landmark', () => {
    render(
      <ActivityStatus
        title="Assistant drafting proposal"
        description="The assistant is generating a proposal."
        tone="info"
        isExecuting={true}
      />
    );
    const status = screen.getByRole('region', { name: 'Assistant drafting proposal' });
    expect(status).toHaveAttribute('data-executing', 'true');
    expect(status).toHaveTextContent('Assistant drafting proposal');
    expect(status).toHaveTextContent('The assistant is generating a proposal.');
    // Check that spinner has reduced motion handling
    const spinner = status.querySelector('svg.animate-spin');
    expect(spinner).toBeInTheDocument();
    expect(spinner).toHaveClass('motion-reduce:animate-none');
  });

  test('waiting state renders static icon without active spinner', () => {
    render(
      <ActivityStatus
        title="Waiting for model quota"
        description="Quota limit reached. Waiting for capacity."
        tone="warning"
        isWaiting={true}
        isExecuting={false}
      />
    );
    const status = screen.getByRole('region', { name: 'Waiting for model quota' });
    expect(status).toHaveAttribute('data-executing', 'false');
    expect(status.querySelector('svg.animate-spin')).not.toBeInTheDocument();
  });

  test('danger / failure tone uses alert role and provides next action', () => {
    render(
      <ActivityStatus
        title="Assistant job failed"
        description="Model failed to start."
        tone="danger"
        isExecuting={false}
        actionRequired="Check client configuration and retry."
      />
    );
    const alert = screen.getByRole('alert');
    expect(alert).toHaveAttribute('aria-live', 'assertive');
    expect(alert).toHaveTextContent('Next action: Check client configuration and retry.');
    expect(alert.querySelector('svg.animate-spin')).not.toBeInTheDocument();
  });

  test('inline variant renders compact layout', () => {
    render(
      <ActivityStatus
        variant="inline"
        title="Running"
        description="In progress"
        tone="info"
        isExecuting={true}
      />
    );
    expect(screen.getByRole('region', { name: 'Running' })).toHaveClass('inline-flex');
    expect(screen.getByText('Running')).toBeInTheDocument();
  });

  test('ActivitySpinner renders accessible reduced-motion-safe indicator', () => {
    render(<ActivitySpinner label="Loading items…" />);
    const spinner = screen.getByRole('status');
    expect(spinner).toHaveTextContent('Loading items…');
    const svg = spinner.querySelector('svg');
    expect(svg).toHaveClass('animate-spin');
    expect(svg).toHaveClass('motion-reduce:animate-none');
  });
});
