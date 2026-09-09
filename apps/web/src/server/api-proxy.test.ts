// @vitest-environment node
import { createServer, type Server, type RequestListener } from "node:http";
import { once } from "node:events";
import { afterEach, expect, test } from "vitest";
import { proxyApi } from "./api-proxy";

const servers: Server[] = [];
afterEach(async () => {
  for (const server of servers.splice(0)) {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});
async function upstream(handler: RequestListener) {
  const server = createServer(handler);
  servers.push(server);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("missing address");
  return `http://127.0.0.1:${address.port}`;
}
const webOrigin = "http://127.0.0.1:3000";

test.each([302, 204, 205, 304])('closes held-open terminal upstream responses (%i)', async status => {
  let closed = false;
  const internalOrigin = await upstream((_req, res) => {
    res.on('close', () => { closed = true; });
    res.writeHead(status, status === 302 ? { Location: 'http://[' } : {});
    res.flushHeaders();
  });
  const response = await proxyApi(new Request(`${webOrigin}/api/runs`, {
    headers: { Host: '127.0.0.1:3000' },
  }), ['runs'], { webOrigin, internalOrigin });
  expect(response.status).toBe(status === 302 ? 502 : status);
  await new Promise(resolve => setTimeout(resolve, 50));
  expect(closed).toBe(true);
});

test("preserves external Host, Origin, session and mutation headers", async () => {
  let observed: Record<string, unknown> = {};
  const internalOrigin = await upstream((req, res) => {
    observed = { host: req.headers.host, origin: req.headers.origin, cookie: req.headers.cookie,
      csrf: req.headers["x-csrf-token"], key: req.headers["idempotency-key"], url: req.url };
    res.setHeader("Set-Cookie", "forge_session=test; HttpOnly; SameSite=Strict; Path=/");
    res.setHeader("Content-Type", "application/json");
    res.end('{"accepted":true}');
  });
  const request = new Request(`${webOrigin}/api/runs?offset=2`, {
    method: "POST", headers: { Host: "127.0.0.1:3000", Origin: webOrigin,
      Cookie: "forge_session=test", "X-CSRF-Token": "csrf", "Idempotency-Key": "request-1" },
    body: '{"task_id":"test"}',
  });
  const response = await proxyApi(request, ["runs"], { webOrigin, internalOrigin });
  expect(response.status).toBe(200);
  expect(await response.json()).toEqual({ accepted: true });
  expect(observed).toEqual({ host: "127.0.0.1:3000", origin: webOrigin,
    cookie: "forge_session=test", csrf: "csrf", key: "request-1", url: "/api/runs?offset=2" });
  expect(response.headers.get("set-cookie")).toContain("HttpOnly");
});

test("rejects foreign Host/Origin, path escapes and oversized requests before forwarding", async () => {
  let calls = 0;
  const internalOrigin = await upstream((_req, res) => { calls++; res.end(); });
  const invalidHeaders: Record<string, string>[] = [{ Host: "evil.example" }, { Host: "127.0.0.1:3000", Origin: "http://127.0.0.1:4000" }];
  for (const headers of invalidHeaders) {
    const response = await proxyApi(new Request(`${webOrigin}/api/runs`, { headers }), ["runs"], { webOrigin, internalOrigin });
    expect(response.status).toBe(403);
  }
  const escape = await proxyApi(new Request(`${webOrigin}/api/runs`, { headers: { Host: "127.0.0.1:3000" } }), ["..", "secret"], { webOrigin, internalOrigin });
  expect(escape.status).toBe(400);
  const large = await proxyApi(new Request(`${webOrigin}/api/runs`, { method: "POST", headers: { Host: "127.0.0.1:3000", Origin: webOrigin }, body: "too big" }), ["runs"], { webOrigin, internalOrigin, maxBodyBytes: 2 });
  expect(large.status).toBe(413);
  expect(calls).toBe(0);
});

test("streams events beyond header timeout and propagates client abort", async () => {
  let closed!: () => void;
  const upstreamClosed = new Promise<void>(resolve => { closed = resolve; });
  let writeNext!: () => void;
  const internalOrigin = await upstream((_req, res) => {
    res.setHeader('Content-Type', 'text/event-stream');
    res.write('id: 1\ndata: first\n\n');
    writeNext = () => res.write('id: 2\ndata: second\n\n');
    res.on('close', closed);
  });
  const abort = new AbortController();
  const response = await proxyApi(new Request(`${webOrigin}/api/events`, {
    headers: { Host: '127.0.0.1:3000', 'Last-Event-ID': '0' }, signal: abort.signal,
  }), ['events'], { webOrigin, internalOrigin, headerTimeoutMs: 100 });
  const reader = response.body!.getReader();
  expect(new TextDecoder().decode((await reader.read()).value)).toContain('first');
  await new Promise(resolve => setTimeout(resolve, 150));
  writeNext();
  expect(new TextDecoder().decode((await reader.read()).value)).toContain('second');
  abort.abort();
  await expect(reader.read()).rejects.toThrow();
  await upstreamClosed;
});

test("bounds header wait, refuses non-loopback destinations and does not follow redirects", async () => {
  const request = () => new Request(`${webOrigin}/api/runs`, { headers: { Host: '127.0.0.1:3000' } });
  const internalOrigin = await upstream(() => {});
  expect((await proxyApi(request(), ['runs'], { webOrigin, internalOrigin, headerTimeoutMs: 20 })).status).toBe(504);
  expect((await proxyApi(request(), ['runs'], { webOrigin, internalOrigin: 'https://example.com' })).status).toBe(503);
  const redirectOrigin = await upstream((_req, res) => {
    res.writeHead(307, { Location: 'https://example.com/steal' }); res.end();
  });
  expect((await proxyApi(request(), ['runs'], { webOrigin, internalOrigin: redirectOrigin })).status).toBe(502);
});

test("bounds streamed request bytes and rejects mutations without Origin", async () => {
  let calls = 0;
  const internalOrigin = await upstream((_req, res) => { calls++; res.end(); });
  const options = { webOrigin, internalOrigin, maxBodyBytes: 3 };
  const missingOrigin = new Request(`${webOrigin}/api/runs`, { method: 'POST', headers: { Host: '127.0.0.1:3000' } });
  expect((await proxyApi(missingOrigin, ['runs'], options)).status).toBe(403);
  let canceled = false;
  const stream = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new Uint8Array(2)); controller.enqueue(new Uint8Array(2)); },
    cancel() { canceled = true; },
  });
  const request = new Request(`${webOrigin}/api/runs`, {
    method: 'POST', headers: { Host: '127.0.0.1:3000', Origin: webOrigin },
    body: stream, duplex: 'half',
  } as RequestInit);
  expect((await proxyApi(request, ['runs'], options)).status).toBe(413);
  expect(canceled).toBe(true);
  expect(calls).toBe(0);
});

test("canceling the downstream event reader closes the upstream connection", async () => {
  let closed!: () => void;
  const upstreamClosed = new Promise<void>(resolve => { closed = resolve; });
  const internalOrigin = await upstream((_req, res) => {
    res.setHeader('Content-Type', 'text/event-stream');
    res.write('data: hello\n\n');
    res.on('close', closed);
  });
  const response = await proxyApi(new Request(`${webOrigin}/api/events`, {
    headers: { Host: '127.0.0.1:3000' },
  }), ['events'], { webOrigin, internalOrigin });
  const reader = response.body!.getReader();
  await reader.read();
  await reader.cancel();
  await upstreamClosed;
});

test('stalled uploads time out and cancel before contacting the API', async () => {
  let calls = 0;
  let canceled = false;
  const internalOrigin = await upstream((_req, res) => { calls++; res.end(); });
  const body = new ReadableStream<Uint8Array>({ cancel() { canceled = true; } });
  const request = new Request(`${webOrigin}/api/runs`, {
    method: 'POST', headers: { Host: '127.0.0.1:3000', Origin: webOrigin }, body, duplex: 'half',
  } as RequestInit);
  const response = await proxyApi(request, ['runs'], { webOrigin, internalOrigin, bodyTimeoutMs: 20 });
  expect(response.status).toBe(408);
  expect(canceled).toBe(true);
  expect(calls).toBe(0);
});
