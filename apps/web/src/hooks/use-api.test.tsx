import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { useApi } from './use-api';

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });
test('switching resources cancels the old request and ignores its late response', async () => {
  let old!: (response: Response) => void;
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementationOnce(() => new Promise(resolve => { old = resolve; }))
    .mockResolvedValueOnce(new Response('{"id":"second"}'));
  const hook = renderHook(({ path }) => useApi<{ id: string }>(path), { initialProps: { path: '/projects/first' } });
  const signal = fetcher.mock.calls[0][1]?.signal;
  hook.rerender({ path: '/projects/second' });
  expect(signal?.aborted).toBe(true);
  await waitFor(() => expect(hook.result.current.value?.id).toBe('second'));
  await act(async () => old(new Response('{"id":"first"}')));
  expect(hook.result.current.value?.id).toBe('second');
});

test('background refresh keeps the snapshot and never overlaps a pending request', async () => {
  vi.useFakeTimers();
  let complete!: (response: Response) => void;
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response('{"id":"first"}'))
    .mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
  const hook = renderHook(() => useApi<{ id: string }>('/tasks', { refreshIntervalMs: 5000, keepPreviousOnRefresh: true }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(hook.result.current.value?.id).toBe('first');
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(hook.result.current.refreshing).toBe(true);
  expect(hook.result.current.loading).toBe(false);
  expect(hook.result.current.value?.id).toBe('first');
  await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
  expect(fetcher.mock.calls).toHaveLength(2);
  await act(async () => { complete(new Response('{"id":"second"}')); });
  expect(hook.result.current.value?.id).toBe('second');
  hook.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(fetcher.mock.calls).toHaveLength(2);
});

test('refresh synchronously returns exact request tokens and retains the displayed token until that request installs', async () => {
  vi.useFakeTimers();
  let complete!: (response: Response) => void;
  vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response('{"id":"first"}'))
    .mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }))
    .mockResolvedValueOnce(new Response('{"id":"first"}'));

  const hook = renderHook(() => useApi<{ id: string }>('/tasks', { keepPreviousOnRefresh: true }));
  expect(hook.result.current.token).toBe(0);

  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(hook.result.current.value?.id).toBe('first');
  expect(hook.result.current.token).toBe(0);

  let target = 0;
  act(() => { target = hook.result.current.refresh(); });
  expect(target).toBe(1);
  expect(hook.result.current.refreshing).toBe(true);
  expect(hook.result.current.value?.id).toBe('first');
  expect(hook.result.current.token).toBe(0);

  await act(async () => { complete(new Response('{"id":"first"}')); });
  expect(hook.result.current.value?.id).toBe('first');
  expect(hook.result.current.token).toBe(target);

  let later = 0;
  act(() => { later = hook.result.current.refresh(); });
  expect(later).toBe(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(hook.result.current.value?.id).toBe('first');
  expect(hook.result.current.token).toBe(later);
});

test('an opted-in refresh is shared across tabs without broadcasting background polling', async () => {
  const storage = vi.spyOn(Storage.prototype, 'setItem');
  const fetcher = vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response('{"id":"first"}'))
    .mockResolvedValueOnce(new Response('{"id":"second"}'))
    .mockResolvedValueOnce(new Response('{"id":"third"}'));
  const hook = renderHook(() => useApi<{ id: string }>('/readiness', {
    keepPreviousOnRefresh: true,
    refreshStorageKey: 'forge:test:refresh',
  }));
  await waitFor(() => expect(hook.result.current.value?.id).toBe('first'));

  act(() => { hook.result.current.refresh(); });
  await waitFor(() => expect(hook.result.current.value?.id).toBe('second'));
  expect(storage).toHaveBeenCalledTimes(1);
  expect(storage.mock.calls[0][0]).toBe('forge:test:refresh');

  act(() => window.dispatchEvent(new StorageEvent('storage', {
    key: 'forge:test:refresh', newValue: 'another-tab',
  })));
  await waitFor(() => expect(hook.result.current.value?.id).toBe('third'));
  expect(fetcher).toHaveBeenCalledTimes(3);
  expect(storage).toHaveBeenCalledTimes(1);
});
