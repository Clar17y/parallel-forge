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
