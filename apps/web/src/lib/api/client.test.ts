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

test('conflict recognition maps explicit safe stale-project-policy code and falls back safely', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'stale-project-policy' }), { status: 409 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'unexpected server error detail' }), { status: 409 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ code: 'stale-project-policy' }), { status: 409 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: 'stale-project-policy' } }), { status: 409 }))
    .mockResolvedValueOnce(new Response('not-valid-json', { status: 409 }));
  await expect(mutate('/runs/one/commands', { command_type: 'resume' }, { idempotencyKey: 'k1' })).rejects.toMatchObject({ status: 409, code: 'stale-project-policy' });
  await expect(mutate('/runs/one/commands', { command_type: 'resume' }, { idempotencyKey: 'k2' })).rejects.toMatchObject({ status: 409, code: 'stale-projection' });
  await expect(mutate('/runs/one/commands', { command_type: 'resume' }, { idempotencyKey: 'k3' })).rejects.toMatchObject({ status: 409, code: 'stale-projection' });
  await expect(mutate('/runs/one/commands', { command_type: 'resume' }, { idempotencyKey: 'k4' })).rejects.toMatchObject({ status: 409, code: 'stale-projection' });
  await expect(mutate('/runs/one/commands', { command_type: 'resume' }, { idempotencyKey: 'k5' })).rejects.toMatchObject({ status: 409, code: 'stale-projection' });
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
  await expect(api('/projects')).rejects.toMatchObject({ status: 422, code: 'request-failed', fields: {} });
  expect(canceled).toBe(true);
});

test('422 recognition maps exact invalid-run-profile detail and keeps other 422s as request-failed', async () => {
  vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'invalid-run-profile' }), { status: 422 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'request cannot be processed' }), { status: 422 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ code: 'invalid-run-profile' }), { status: 422 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: 'invalid-run-profile' } }), { status: 422 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'INVALID-RUN-PROFILE' }), { status: 422 }))
    .mockResolvedValueOnce(new Response('not-valid-json', { status: 422 }));
  await expect(api('/runs')).rejects.toMatchObject({ status: 422, code: 'invalid-run-profile', fields: {} });
  await expect(api('/runs')).rejects.toMatchObject({ status: 422, code: 'request-failed', fields: {} });
  await expect(api('/runs')).rejects.toMatchObject({ status: 422, code: 'request-failed', fields: {} });
  await expect(api('/runs')).rejects.toMatchObject({ status: 422, code: 'request-failed', fields: {} });
  await expect(api('/runs')).rejects.toMatchObject({ status: 422, code: 'request-failed', fields: {} });
  await expect(api('/runs')).rejects.toMatchObject({ status: 422, code: 'request-failed', fields: {} });
});

test('mutation supports PUT method for budget and idempotency header', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ version: 2 }), { status: 200 }));
  const result = await mutate<{ version: number }>('/epics/one/budget', { expected_version: 1 }, {
    idempotencyKey: 'put-key',
    method: 'PUT',
  });
  expect(result).toEqual({ version: 2 });
  const options = fetcher.mock.calls[0][1]!;
  expect(options.method).toBe('PUT');
  const headers = new Headers(options.headers);
  expect(headers.get('Idempotency-Key')).toBe('put-key');
});
