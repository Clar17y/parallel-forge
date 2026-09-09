import { afterEach, expect, test, vi } from "vitest";
import { api, ApiError, mutate } from "./client";
import { csrf } from './csrf';
afterEach(() => { vi.restoreAllMocks(); csrf.clear(); });
test("client uses relative same-origin API and maps auth failures", async () => { const fetcher=vi.spyOn(globalThis,"fetch").mockResolvedValue(new Response("",{status:401})); await expect(api("/runs")).rejects.toBeInstanceOf(ApiError); expect(fetcher.mock.calls[0][0]).toBe("/api/runs"); expect(fetcher.mock.calls[0][1]).toMatchObject({credentials:"same-origin"}); });

test('mutation binds CSRF, idempotency and expected version; conflicts stay structured', async () => {
  csrf.set('memory-only');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 409 }));
  await expect(mutate('/runs/one/commands', { command: 'pause' }, { idempotencyKey: 'stable-key', expectedVersion: 7 })).rejects.toMatchObject({ code: 'stale-projection' });
  const options = fetcher.mock.calls[0][1]!;
  const headers = new Headers(options.headers);
  expect(headers.get('X-CSRF-Token')).toBe('memory-only');
  expect(headers.get('Idempotency-Key')).toBe('stable-key');
  expect(JSON.parse(options.body as string)).toEqual({ command: 'pause', expected_run_version: 7 });
});

test('validation errors expose bounded field identifiers without raw messages or input', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: [
    { loc: ['body', 'name'], type: 'string_too_short', msg: 'private server text', input: 'secret input' },
  ] }), { status: 422 }));
  await expect(api('/projects')).rejects.toMatchObject({ fields: { name: 'Invalid value.' } });
  try { await api('/projects'); } catch (error) { expect(JSON.stringify(error)).not.toContain('secret'); }
});

test('rejects path escapes before fetch and clears CSRF on expired session', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 401 }));
  await expect(api('/../outside')).rejects.toThrow();
  expect(fetcher).not.toHaveBeenCalled();
  csrf.set('stale');
  await expect(api('/runs')).rejects.toMatchObject({ code: 'bootstrap-required' });
  expect(csrf.get()).toBeNull();
});

test('oversized structured errors are canceled without retaining server detail', async () => {
  let canceled = false;
  const body = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new Uint8Array(16_385)); },
    cancel() { canceled = true; },
  });
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(body, { status: 422 }));
  await expect(api('/projects')).rejects.toMatchObject({ fields: {} });
  expect(canceled).toBe(true);
});
