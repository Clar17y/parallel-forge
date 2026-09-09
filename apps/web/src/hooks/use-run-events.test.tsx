import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { useRunEvents } from './use-run-events';
import { csrf } from '@/lib/api/csrf';

afterEach(() => { cleanup(); vi.restoreAllMocks(); csrf.clear(); });
function stream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  return { response: new Response(new ReadableStream<Uint8Array>({ start(value) { controller = value; } }),
    { headers: { 'Content-Type': 'text/event-stream' } }),
  event(sequence: number) { controller.enqueue(new TextEncoder().encode(`id: ${sequence}\nevent: run.updated\ndata: {"sequence":${sequence}}\n\n`)); },
  close() { controller.close(); } };
}

test('reconnects at the exact cursor, ignores duplicates and refreshes after sequence gaps', async () => {
  const first = stream(); const second = stream();
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(first.response).mockResolvedValueOnce(second.response);
  const refresh = vi.fn();
  renderHook(() => useRunEvents('run-1', '6', refresh, { retryMs: 5 }));
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  await act(async () => first.event(7));
  expect(refresh).toHaveBeenCalledTimes(2);
  await act(async () => first.close());
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  expect(new Headers(fetcher.mock.calls[1][1]?.headers).get('Last-Event-ID')).toBe('7');
  const before = refresh.mock.calls.length;
  await act(async () => { second.event(7); second.event(8); second.event(10); });
  expect(refresh).toHaveBeenCalledTimes(before + 2);
});

test('unmount aborts the stream and 401 terminates retries and clears memory authentication', async () => {
  const events = stream();
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(events.response).mockResolvedValueOnce(new Response('', { status: 401 }));
  const first = renderHook(() => useRunEvents('run-1', '0', vi.fn()));
  await waitFor(() => expect(first.result.current).toBe('connected'));
  const signal = fetcher.mock.calls[0][1]?.signal;
  first.unmount();
  expect(signal?.aborted).toBe(true);
  csrf.set('memory-token');
  const second = renderHook(() => useRunEvents('run-2', '0', vi.fn(), { retryMs: 5 }));
  await waitFor(() => expect(second.result.current).toBe('unavailable'));
  expect(csrf.get()).toBeNull();
  expect(fetcher).toHaveBeenCalledTimes(2);
});
