import { csrf } from './csrf';

export class ApiError extends Error {
  constructor(public status: number, public code: string, public fields: Record<string, string> = {}) {
    super(code);
  }
}

function apiPath(path: string): string {
  const relative = path.startsWith('/') ? path : `/${path}`;
  const pathname = relative.split('?')[0];
  if (!pathname || pathname.split('/').slice(1).some(part =>
    !/^[A-Za-z0-9_.-]+$/.test(part) || part === '.' || part === '..') || relative.includes('#')) {
    throw new ApiError(0, 'invalid-api-path');
  }
  return `/api${relative}`;
}

async function validationFields(response: Response): Promise<Record<string, string>> {
  const reader = response.body?.getReader();
  if (!reader) return {};
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 16_384) { await reader.cancel(); return {}; }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    const payload: unknown = JSON.parse(new TextDecoder().decode(bytes));
    if (!payload || typeof payload !== 'object' || !('detail' in payload) || !Array.isArray(payload.detail)) return {};
    const fields: Record<string, string> = {};
    for (const item of payload.detail.slice(0, 32)) {
      if (!item || typeof item !== 'object' || !Array.isArray(item.loc)) continue;
      const parts: unknown[] = item.loc[0] === 'body' ? item.loc.slice(1) : item.loc;
      if (!parts.length || parts.length > 8 || !parts.every(part =>
        typeof part === 'number' ? Number.isSafeInteger(part) && part >= 0 :
        typeof part === 'string' && /^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(part))) continue;
      fields[parts.join('.')] = 'Invalid value.';
    }
    return fields;
  } catch { return {}; }
  finally { reader.releaseLock(); }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T | undefined> {
  const url = apiPath(path);
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const token = csrf.get();
    if (token) headers.set('X-CSRF-Token', token);
  }
  const response = await fetch(url, { ...init, method, headers, credentials: 'same-origin' });
  if (!response.ok) {
    if (response.status === 401) { csrf.clear(); throw new ApiError(401, 'bootstrap-required'); }
    if (response.status === 409) throw new ApiError(409, 'stale-projection');
    throw new ApiError(response.status, 'request-failed', response.status === 422 ? await validationFields(response) : {});
  }
  if (response.status === 204 || method === 'HEAD') return undefined;
  return response.json() as Promise<T>;
}

export function mutate<T>(path: string, body: Record<string, unknown>, options: {
  idempotencyKey: string;
  expectedVersion?: number;
  signal?: AbortSignal;
}): Promise<T | undefined> {
  const payload = options.expectedVersion === undefined ? body : { ...body, expected_run_version: options.expectedVersion };
  return api<T>(path, {
    method: 'POST', signal: options.signal,
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': options.idempotencyKey },
    body: JSON.stringify(payload),
  });
}
