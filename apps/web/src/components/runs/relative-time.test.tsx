import { act, cleanup, render, screen } from '@testing-library/react';
import { renderToString } from 'react-dom/server';
import { afterEach, expect, test, vi } from 'vitest';
import { RelativeTime } from './relative-time';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

test('renders a neutral server time and hydrates to the live shared clock', () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-09-28T12:00:00Z'));
  const element = <RelativeTime timestamp="2026-09-28T11:59:30Z" />;
  const container = document.createElement('div');
  container.innerHTML = renderToString(element);
  document.body.appendChild(container);
  expect(container.textContent).toBe('Loading time…');
  expect(container.querySelector('time')).toHaveAttribute('datetime', '2026-09-28T11:59:30Z');
  expect(container.querySelector('time')).not.toHaveAttribute('title');
  expect(vi.getTimerCount()).toBe(0);

  const onRecoverableError = vi.fn();
  const view = render(element, { container, hydrate: true, onRecoverableError });
  expect(screen.getByText('30s ago')).toBeInTheDocument();
  expect(onRecoverableError).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(1);
  act(() => vi.advanceTimersByTime(30_000));
  expect(screen.getByText('1m ago')).toBeInTheDocument();
  view.unmount();
  expect(vi.getTimerCount()).toBe(0);
});

test.each([undefined, 'not-a-date'])('keeps invalid server timestamps neutral: %s', timestamp => {
  const container = document.createElement('div');
  container.innerHTML = renderToString(<RelativeTime timestamp={timestamp} />);
  expect(container.textContent).toBe('Unknown time');
  expect(container.querySelector('time')).not.toHaveAttribute('datetime');
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
