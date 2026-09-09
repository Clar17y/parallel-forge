import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { useApi } from './use-api';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
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
