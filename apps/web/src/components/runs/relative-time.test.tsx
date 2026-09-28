import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { RelativeTime } from './relative-time';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test('renders malformed and missing times without throwing', () => {
  render(<><RelativeTime timestamp="not-a-date" /><RelativeTime timestamp={undefined} /></>);
  expect(screen.getAllByText('Unknown time')).toHaveLength(2);
  expect(screen.getAllByTitle('Unknown time')).toHaveLength(2);
});

test('shares its ticking interval across rows and cleans up on unmount', () => {
  const set = vi.spyOn(globalThis, 'setInterval');
  const clear = vi.spyOn(globalThis, 'clearInterval');
  const future = new Date(Date.now() + 120_000).toISOString();
  const view = render(<><RelativeTime timestamp={future} /><RelativeTime timestamp={future} /></>);
  expect(screen.getAllByText(/in 2m/)).toHaveLength(2);
  expect(set).toHaveBeenCalledTimes(1);
  view.unmount();
  expect(clear).toHaveBeenCalledTimes(1);
});
