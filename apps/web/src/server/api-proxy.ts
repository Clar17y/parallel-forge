import { request as httpRequest, type IncomingMessage } from 'node:http';
import { request as httpsRequest } from 'node:https';
import { Readable } from 'node:stream';

/** Fixed-destination same-origin bridge. Never accept a destination from callers. */
type ProxyOptions = {
  webOrigin: string;
  internalOrigin: string;
  maxBodyBytes?: number;
  bodyTimeoutMs?: number;
  headerTimeoutMs?: number;
};

function loopbackOrigin(value: string): URL {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) ||
      !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) ||
      url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('Expected a loopback origin');
  }
  return url;
}

function failure(status: number, detail: string): Response {
  return Response.json({ detail }, { status, headers: { 'Cache-Control': 'no-store' } });
}

async function boundedBody(request: Request, limit: number, signal: AbortSignal): Promise<Uint8Array | undefined> {
  if (!request.body) return undefined;
  const reader = request.body.getReader();
  const cancel = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', cancel, { once: true });
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    if (signal.aborted) { cancel(); throw new Error('Request aborted'); }
    while (true) {
      const { done, value } = await reader.read();
      if (signal.aborted) throw new Error('Request aborted');
      if (done) break;
      size += value.byteLength;
      if (size > limit) {
        cancel();
        throw new RangeError('Request too large');
      }
      chunks.push(value);
    }
  } finally {
    signal.removeEventListener('abort', cancel);
    reader.releaseLock();
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
  return body;
}

export async function proxyApi(request: Request, path: string[], options: ProxyOptions): Promise<Response> {
  let external: URL;
  let internal: URL;
  try {
    external = loopbackOrigin(options.webOrigin);
    internal = loopbackOrigin(options.internalOrigin);
  } catch { return failure(503, 'API proxy is not configured'); }
  const origin = request.headers.get('origin');
  const mutation = !['GET', 'HEAD', 'OPTIONS'].includes(request.method);
  if (request.headers.get('host') !== external.host ||
      (origin !== null && origin !== external.origin) || (mutation && origin === null)) {
    return failure(403, 'Invalid request origin');
  }
  if (!path.length || path.some(segment => !/^[A-Za-z0-9_.-]+$/.test(segment) || segment === '.' || segment === '..')) {
    return failure(400, 'Invalid API path');
  }
  const limit = options.maxBodyBytes ?? 8 * 1024 * 1024;
  const declaredLength = request.headers.get('content-length');
  if (declaredLength !== null && (!/^\d+$/.test(declaredLength) || Number(declaredLength) > limit)) {
    return failure(413, 'Request too large');
  }
  let body: Uint8Array | undefined;
  const bodyTimeout = new AbortController();
  const bodyTimer = setTimeout(() => bodyTimeout.abort(), options.bodyTimeoutMs ?? 10_000);
  try { body = await boundedBody(request, limit, AbortSignal.any([request.signal, bodyTimeout.signal])); }
  catch (error) {
    return failure(bodyTimeout.signal.aborted ? 408 : error instanceof RangeError ? 413 : 400, 'Unable to read request');
  } finally { clearTimeout(bodyTimer); }
  const target = new URL(`/api/${path.map(encodeURIComponent).join('/')}`, internal);
  target.search = new URL(request.url).search;
  const headers = new Headers({ Host: external.host });
  for (const name of ['cookie', 'origin', 'content-type', 'accept', 'last-event-id', 'x-csrf-token', 'idempotency-key']) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(), options.headerTimeoutMs ?? 10_000);
  let upstream: IncomingMessage | undefined;
  try {
    // Node fetch overrides Host; the native HTTP client preserves this validated value.
    upstream = await new Promise<IncomingMessage>((resolve, reject) => {
      const send = target.protocol === 'https:' ? httpsRequest : httpRequest;
      const outgoing = send(target, {
        method: request.method, headers: Object.fromEntries(headers),
        signal: AbortSignal.any([request.signal, timeout.signal]),
      }, resolve);
      outgoing.on('error', reject);
      outgoing.end(body);
    });
    clearTimeout(timer); // SSE bodies can remain open beyond the header deadline.
    const responseHeaders = new Headers();
    for (const name of ['content-type', 'content-disposition', 'cache-control', 'vary', 'x-content-type-options', 'cross-origin-resource-policy']) {
      const value = upstream.headers[name];
      if (typeof value === 'string') responseHeaders.set(name, value);
    }
    for (const cookie of upstream.headers['set-cookie'] ?? []) responseHeaders.append('set-cookie', cookie);
    const location = upstream.headers.location;
    if (location) {
      const redirect = new URL(location, target);
      if (![internal.origin, external.origin].includes(redirect.origin)) {
        upstream.destroy();
        return failure(502, 'Invalid API redirect');
      }
      responseHeaders.set('location', redirect.pathname + redirect.search + redirect.hash);
    }
    const status = upstream.statusCode ?? 502;
    const noBody = request.method === 'HEAD' || [204, 205, 304].includes(status);
    if (noBody) upstream.destroy();
    return new Response(noBody ? null : Readable.toWeb(upstream) as ReadableStream<Uint8Array>, {
      status, headers: responseHeaders,
    });
  } catch {
    upstream?.destroy();
    return failure(timeout.signal.aborted ? 504 : 502, 'API is unavailable');
  } finally { clearTimeout(timer); }
}
