import { StrictMode } from 'react';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { BootstrapGate } from './bootstrap-gate';
import { csrf } from '@/lib/api/csrf';

afterEach(() => { cleanup(); csrf.clear(); window.history.replaceState(null, '', '/'); vi.restoreAllMocks(); });

test('StrictMode exchanges once, strips fragment before completion and recovers memory CSRF', async () => {
  window.location.hash = '#bootstrap=one-time';
  let finish!: (value: { csrfToken: string }) => void;
  const exchange = vi.fn(() => new Promise<{ csrfToken: string }>(resolve => { finish = resolve; }));
  const session = vi.fn().mockResolvedValue({ csrfToken: 'recovered' });
  render(<StrictMode><BootstrapGate exchange={exchange} session={session}><div>Dashboard</div></BootstrapGate></StrictMode>);
  expect(window.location.hash).toBe('');
  await act(async () => {});
  expect(exchange).toHaveBeenCalledTimes(1);
  finish({ csrfToken: 'initial' });
  expect(await screen.findByText('Dashboard')).toBeInTheDocument();
  expect(csrf.get()).toBe('recovered');
});

test('reload recovers a live session and malformed fragments fail safely without retaining them', async () => {
  const session = vi.fn().mockResolvedValue({ csrfToken: 'recovered' });
  const view = render(<BootstrapGate session={session}><div>Dashboard</div></BootstrapGate>);
  await screen.findByText('Dashboard');
  expect(csrf.get()).toBe('recovered');
  view.unmount();
  window.location.hash = '#bootstrap=%ZZ';
  render(<BootstrapGate session={session}><div>Dashboard</div></BootstrapGate>);
  expect(await screen.findByText('Sign-in required')).toBeInTheDocument();
  expect(screen.getByRole('main')).toContainElement(screen.getByRole('alert'));
  expect(window.location.hash).toBe('');
  expect(csrf.get()).toBeNull();
});

test('unmounted bootstrap cannot install its token', async () => {
  let finish!: (value: { csrfToken: string }) => void;
  const session = () => new Promise<{ csrfToken: string }>(resolve => { finish = resolve; });
  const view = render(<BootstrapGate session={session}><div>Dashboard</div></BootstrapGate>);
  await act(async () => {});
  view.unmount();
  finish({ csrfToken: 'late' });
  await Promise.resolve();
  expect(csrf.get()).toBeNull();
});

test('default wiring uses session recovery and invalidates the shell when authentication expires', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response('{}'))
    .mockResolvedValueOnce(new Response('{"csrf_token":"recovered"}'));
  render(<BootstrapGate><div>Dashboard</div></BootstrapGate>);
  await screen.findByText('Dashboard');
  expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/auth/session', '/api/auth/csrf']);
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
  act(() => csrf.clear());
  expect(screen.getByText('Sign-in required')).toBeInTheDocument();
  expect(screen.queryByText('Dashboard')).not.toBeInTheDocument();
});

test('invalidation during initialization is terminal even if the old promise resolves', async () => {
  let finish!: (value: { csrfToken: string }) => void;
  const session = () => new Promise<{ csrfToken: string }>(resolve => { finish = resolve; });
  render(<BootstrapGate session={session}><div>Dashboard</div></BootstrapGate>);
  await act(async () => {});
  act(() => csrf.clear());
  await act(async () => finish({ csrfToken: 'stale' }));
  expect(screen.queryByText('Dashboard')).not.toBeInTheDocument();
  expect(csrf.get()).toBeNull();
});

test('unmount cancels the actual bootstrap fetch before another gate starts', async () => {
  window.location.hash = '#bootstrap=first';
  let signal: AbortSignal | undefined;
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementationOnce((_url, init) => {
    signal = init?.signal as AbortSignal;
    return new Promise((_resolve, reject) => {
      signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    });
  });
  const first = render(<BootstrapGate><div>First dashboard</div></BootstrapGate>);
  await act(async () => {});
  first.unmount();
  expect(signal?.aborted).toBe(true);
  fetcher.mockResolvedValueOnce(new Response('{}'))
    .mockResolvedValueOnce(new Response('{"csrf_token":"second"}'));
  render(<BootstrapGate><div>Second dashboard</div></BootstrapGate>);
  await screen.findByText('Second dashboard');
  expect(csrf.get()).toBe('second');
});
