import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';
import { SetupChecklist } from './setup-checklist';

afterEach(cleanup);

function step(name: RegExp) {
  const item = screen.getByText(name).closest('li');
  if (!item) throw new Error(`Step not rendered as a list item: ${name}`);
  return item;
}

test('a fresh project is guided: registration done, model choice next, first run after', () => {
  render(<SetupChecklist profileSelected={false} hasRuns={false} />);
  expect(screen.getByRole('heading', { name: 'Get set up' })).toBeInTheDocument();
  expect(step(/Register your project/)).toHaveTextContent('Done');
  expect(step(/Choose how agents run/)).toHaveTextContent('Next');
  expect(step(/Start your first run/)).toHaveTextContent('To do');
  expect(screen.getByRole('link', { name: 'Manage profiles' })).toHaveAttribute('href', '/subscription-profiles');
  expect(screen.getByRole('link', { name: 'Start a run' })).toHaveAttribute('href', '/runs/new');
});

test('once a run exists the checklist gets out of the way, with or without a profile', () => {
  const { rerender } = render(<SetupChecklist profileSelected hasRuns />);
  expect(screen.getByText(/Setup complete/)).toBeInTheDocument();
  expect(screen.queryByText('Next')).not.toBeInTheDocument();
  rerender(<SetupChecklist profileSelected={false} hasRuns />);
  expect(screen.getByText(/Setup complete/)).toBeInTheDocument();
});

test('unknown run history never claims the first run happened', () => {
  render(<SetupChecklist profileSelected={false} hasRuns={null} />);
  expect(screen.queryByText(/Setup complete/)).not.toBeInTheDocument();
  expect(step(/Start your first run/)).not.toHaveTextContent('Done');
});
