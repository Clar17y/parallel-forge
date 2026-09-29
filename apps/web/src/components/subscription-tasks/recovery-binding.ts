import type { components } from '@/lib/api/schema';

type Body = components['schemas']['RecoveryApplyRequest'];
export type RecoveryBinding = {
  actorId: string; runId: string; taskId: string; attemptId: string;
  key: string; body: Body;
};
type ReadResult = { binding: RecoveryBinding | null; error: boolean };
const prefix = 'forge:recovery:v1:';
const idPattern = /^[A-Za-z0-9_.-]{1,128}$/;
const actions = new Set(['retry_application', 'reject_and_retry_step', 'repair_approved_plan_contract']);

export function bindingStorageKey(actorId: string, runId: string, taskId: string, attemptId: string): string | null {
  if (![actorId, runId, taskId, attemptId].every(value => idPattern.test(value))) return null;
  return prefix + [actorId, runId, taskId, attemptId].map(encodeURIComponent).join(':');
}

function valid(value: unknown, identity: Omit<RecoveryBinding, 'key' | 'body'>): value is RecoveryBinding {
  if (!value || typeof value !== 'object') return false;
  const entry = value as Partial<RecoveryBinding>;
  const body = entry.body;
  return entry.actorId === identity.actorId && entry.runId === identity.runId
    && entry.taskId === identity.taskId && entry.attemptId === identity.attemptId
    && typeof entry.key === 'string' && /^[0-9a-f-]{36}$/i.test(entry.key)
    && !!body && typeof body === 'object' && actions.has(body.action)
    && typeof body.preview_token === 'string' && body.preview_token.length > 0 && body.preview_token.length <= 8192
    && typeof body.reason === 'string' && body.reason.trim().length > 0
    && new TextEncoder().encode(body.reason).length <= 512;
}

export function readBinding(identity: Omit<RecoveryBinding, 'key' | 'body'>): ReadResult {
  const key = bindingStorageKey(identity.actorId, identity.runId, identity.taskId, identity.attemptId);
  if (!key) return { binding: null, error: true };
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return { binding: null, error: false };
    if (raw.length > 12_000) return { binding: null, error: true };
    const parsed: unknown = JSON.parse(raw);
    return valid(parsed, identity) ? { binding: parsed, error: false } : { binding: null, error: true };
  } catch { return { binding: null, error: true }; }
}

export function saveBinding(binding: RecoveryBinding): boolean {
  const key = bindingStorageKey(binding.actorId, binding.runId, binding.taskId, binding.attemptId);
  if (!key || !valid(binding, binding)) return false;
  try {
    if (localStorage.getItem(key) !== null) return false;
    localStorage.setItem(key, JSON.stringify(binding));
    return localStorage.getItem(key) === JSON.stringify(binding);
  } catch { return false; }
}

export async function reserveBinding(binding: RecoveryBinding, signal?: AbortSignal): Promise<'saved' | 'existing' | 'unavailable'> {
  const key = bindingStorageKey(binding.actorId, binding.runId, binding.taskId, binding.attemptId);
  if (!key || !navigator.locks?.request || signal?.aborted) return 'unavailable';
  try {
    return await navigator.locks.request(key, signal ? { signal } : {}, () => {
      if (signal?.aborted) return 'unavailable';
      const current = readBinding(binding);
      if (current.error) return 'unavailable';
      if (current.binding) return 'existing';
      return saveBinding(binding) ? 'saved' : 'unavailable';
    });
  } catch { return 'unavailable'; }
}

export function matchesStoredBinding(binding: RecoveryBinding): boolean {
  const current = readBinding(binding);
  return !current.error && !!current.binding && sameBinding(current.binding, binding);
}

export const sameBinding = (left: RecoveryBinding, right: RecoveryBinding): boolean =>
  JSON.stringify(left) === JSON.stringify(right);

export async function removeMatchingBinding(binding: RecoveryBinding, signal?: AbortSignal): Promise<boolean> {
  const key = bindingStorageKey(binding.actorId, binding.runId, binding.taskId, binding.attemptId);
  if (!key || !navigator.locks?.request || signal?.aborted) return false;
  try {
    return await navigator.locks.request(key, signal ? { signal } : {}, () => {
      if (signal?.aborted || !matchesStoredBinding(binding)) return false;
      try { localStorage.removeItem(key); return true; } catch { return false; }
    });
  } catch { return false; }
}
