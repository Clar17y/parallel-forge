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

test('displayed values carry their request start and settlement times through refresh and failure', async () => {
  vi.useFakeTimers();
  vi.setSystemTime(100_000);
  let first!: (response: Response) => void;
  let second!: (response: Response) => void;
  vi.spyOn(globalThis, 'fetch')
    .mockImplementationOnce(() => new Promise(resolve => { first = resolve; }))
    .mockImplementationOnce(() => new Promise(resolve => { second = resolve; }))
    .mockResolvedValueOnce(new Response(null, { status: 204 }));
  const hook = renderHook(() => useApi<{ id: string }>('/tasks', {
    keepPreviousOnRefresh: true, keepPreviousOnError: true,
  }));
  expect(hook.result.current.valueStartedAt).toBeUndefined();
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); first(new Response('{"id":"first"}')); });
  expect(hook.result.current.value?.id).toBe('first');
  expect(hook.result.current.valueStartedAt).toBe(100_000);
  expect(hook.result.current.valueSettledAt).toBe(160_000);

  act(() => { hook.result.current.refresh(); });
  expect(hook.result.current.valueStartedAt).toBe(100_000);
  expect(hook.result.current.valueSettledAt).toBe(160_000);
  await act(async () => { await vi.advanceTimersByTimeAsync(5_000); second(new Response('{"id":"second"}')); });
  expect(hook.result.current.value?.id).toBe('second');
  expect(hook.result.current.valueStartedAt).toBe(160_000);
  expect(hook.result.current.valueSettledAt).toBe(165_000);

  act(() => { hook.result.current.refresh(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
  expect(hook.result.current.failed).toBe(true);
  expect(hook.result.current.value?.id).toBe('second');
  expect(hook.result.current.valueStartedAt).toBe(160_000);
  expect(hook.result.current.valueSettledAt).toBe(165_000);
});

test('discarded error values and late responses cannot publish value timing', async () => {
  vi.useFakeTimers();
  vi.setSystemTime(100_000);
  let first!: (response: Response) => void;
  vi.spyOn(globalThis, 'fetch')
    .mockImplementationOnce(() => new Promise(resolve => { first = resolve; }))
    .mockResolvedValueOnce(new Response('{"id":"second"}'))
    .mockResolvedValueOnce(new Response(null, { status: 204 }));
  const hook = renderHook(({ path }) => useApi<{ id: string }>(path, { keepPreviousOnRefresh: true }),
    { initialProps: { path: '/first' } });
  vi.setSystemTime(105_000);
  hook.rerender({ path: '/second' });
  await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
  expect(hook.result.current.value?.id).toBe('second');
  expect(hook.result.current.valueStartedAt).toBe(105_000);
  expect(hook.result.current.valueSettledAt).toBe(105_000);
  await act(async () => { first(new Response('{"id":"late"}')); });
  expect(hook.result.current.value?.id).toBe('second');
  act(() => { hook.result.current.refresh(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(hook.result.current.value).toBeUndefined();
  expect(hook.result.current.valueStartedAt).toBeUndefined();
  expect(hook.result.current.valueSettledAt).toBeUndefined();
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

test('an opted-in failed refresh retains its last successful value and marks it unverified until retry', async () => {
  let failRefresh!: (error: Error) => void;
  const fetcher = vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response('{"id":"first"}'))
    .mockImplementationOnce(() => new Promise((_resolve, reject) => { failRefresh = reject; }))
    .mockResolvedValueOnce(new Response('{"id":"recovered"}'));
  const hook = renderHook(() => useApi<{ id: string }>('/profiles', {
    keepPreviousOnRefresh: true,
    keepPreviousOnError: true,
  }));
  await waitFor(() => expect(hook.result.current.value?.id).toBe('first'));
  const firstStartedAt = hook.result.current.valueStartedAt;
  const firstSettledAt = hook.result.current.valueSettledAt;
  act(() => { hook.result.current.refresh(); });
  expect(hook.result.current.value?.id).toBe('first');
  await act(async () => { failRefresh(new Error('offline')); });
  expect(hook.result.current.failed).toBe(true);
  expect(hook.result.current.value?.id).toBe('first');
  expect(hook.result.current.token).toBe(0);
  expect(hook.result.current.valueStartedAt).toBe(firstStartedAt);
  expect(hook.result.current.valueSettledAt).toBe(firstSettledAt);
  act(() => { hook.result.current.refresh(); });
  await waitFor(() => expect(hook.result.current.value?.id).toBe('recovered'));
  expect(hook.result.current.failed).toBe(false);
  expect(fetcher).toHaveBeenCalledTimes(3);
});

test('retained error data never crosses to another path', async () => {
  vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response('{"id":"first"}'))
    .mockRejectedValueOnce(new Error('offline'));
  const hook = renderHook(({ path }) => useApi<{ id: string }>(path, {
    keepPreviousOnRefresh: true,
    keepPreviousOnError: true,
  }), { initialProps: { path: '/profiles/first' } });
  await waitFor(() => expect(hook.result.current.value?.id).toBe('first'));
  hook.rerender({ path: '/profiles/second' });
  expect(hook.result.current.value).toBeUndefined();
  await waitFor(() => expect(hook.result.current.failed).toBe(true));
  expect(hook.result.current.value).toBeUndefined();
});

test('retained failed value stays stale through a hanging retry and repeated failure until a successful read', async () => {
  let retry!: (response: Response) => void;
  vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response('{"id":"running"}'))
    .mockRejectedValueOnce(new Error('offline'))
    .mockImplementationOnce(() => new Promise(resolve => { retry = resolve; }))
    .mockRejectedValueOnce(new Error('still offline'))
    .mockResolvedValueOnce(new Response('{"id":"proposed"}'));
  const hook = renderHook(() => useApi<{ id: string }>('/jobs/one', {
    keepPreviousOnRefresh: true, keepPreviousOnError: true,
  }));
  await waitFor(() => expect(hook.result.current.value?.id).toBe('running'));
  act(() => { hook.result.current.refresh(); });
  await waitFor(() => expect(hook.result.current.failed).toBe(true));
  expect(hook.result.current.valueStale).toBe(true);
  act(() => { hook.result.current.refresh(); });
  expect(hook.result.current.failed).toBe(false);
  expect(hook.result.current.valueStale).toBe(true);
  await act(async () => retry(new Response(null, { status: 503 })));
  expect(hook.result.current.valueStale).toBe(true);
  act(() => { hook.result.current.refresh(); });
  await waitFor(() => expect(hook.result.current.failed).toBe(true));
  expect(hook.result.current.valueStale).toBe(true);
  act(() => { hook.result.current.refresh(); });
  await waitFor(() => expect(hook.result.current.value?.id).toBe('proposed'));
  expect(hook.result.current.valueStale).toBe(false);
});
