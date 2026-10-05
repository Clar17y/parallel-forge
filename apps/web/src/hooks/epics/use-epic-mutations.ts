'use client';

import { createContext, createElement, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useSyncExternalStore, type ReactNode } from 'react';
import { api, ApiError } from '@/lib/api/client';

export type HttpMethod = 'POST' | 'PATCH';
export interface FrozenMutation {
  kind?: string;
  method: HttpMethod;
  path: string;
  body: Record<string, unknown>;
  idempotencyKey: string;
  timestamp: number;
  uncertain?: boolean;
}
type Completion = (value: unknown, mutation: FrozenMutation) => void;
type Consumer = { isActive: () => boolean };
type RegisteredCompletion = { handler: Completion; consumer: Consumer };
type Scope = {
  loaded: boolean;
  pending: FrozenMutation | null;
  error: string | null;
  conflict: boolean;
  actionKind?: string | null;
  reloadProtected: boolean;
  inFlight: boolean;
  handlers: Map<string, Set<RegisteredCompletion>>;
  listeners: Map<() => void, Consumer>;
  snapshot?: MutationSnapshot;
};

type MutationSnapshot = {
  pendingMutation: FrozenMutation | null;
  hasPendingRetry: boolean;
  loading: boolean;
  conflict: boolean;
  error: string | null;
  actionKind?: string | null;
  reloadProtected: boolean;
};
const INITIAL_SNAPSHOT: MutationSnapshot = {
  pendingMutation: null, hasPendingRetry: false, loading: false,
  conflict: false, error: null, actionKind: null, reloadProtected: true,
};

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function normalizeEpicId(id?: string | null): string | undefined {
  if (!id) return undefined;
  if (UUID_REGEX.test(id)) return id.toLowerCase();
  return id;
}

function freeze<T>(value: T): T {
  if (value && typeof value === 'object') {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

function loadPending(key: string): { pending: FrozenMutation | null; protected: boolean } {
  try {
    let raw = window.sessionStorage.getItem(key);
    if (!raw) {
      const lowerKey = key.toLowerCase();
      raw = lowerKey !== key ? window.sessionStorage.getItem(lowerKey) : null;
    }
    if (!raw) return { pending: null, protected: true };
    const value = JSON.parse(raw) as FrozenMutation;
    if (!value || !['POST', 'PATCH'].includes(value.method) ||
        typeof value.path !== 'string' || typeof value.idempotencyKey !== 'string' ||
        !value.body || typeof value.body !== 'object') throw new Error('Invalid saved request');
    return { pending: freeze({ ...value, uncertain: true }), protected: true };
  } catch {
    return { pending: null, protected: false };
  }
}

// Mutable request ownership lives outside React's render state. Each change
// publishes an immutable snapshot; the synchronous guard also covers callers
// holding callbacks from the previous render.
class EpicMutationStore {
  private scopes = new Map<string, Scope>();

  scope(key: string): Scope {
    let scope = this.scopes.get(key);
    if (!scope) {
      scope = {
        loaded: false,
        pending: null,
        error: null,
        conflict: false,
        actionKind: null,
        reloadProtected: true,
        inFlight: false,
        handlers: new Map(),
        listeners: new Map(),
      };
      this.scopes.set(key, scope);
    }
    return scope;
  }

  subscribe(key: string, listener: () => void, consumer: Consumer) {
    const scope = this.scope(key);
    scope.listeners.set(listener, consumer);
    return () => { scope.listeners.delete(listener); };
  }

  snapshot(key: string): MutationSnapshot {
    const scope = this.scope(key);
    return scope.snapshot ??= Object.freeze({
      pendingMutation: scope.pending, hasPendingRetry: scope.pending !== null,
      loading: scope.inFlight, conflict: scope.conflict, error: scope.error,
      actionKind: scope.actionKind, reloadProtected: scope.reloadProtected,
    });
  }

  private publish(scope: Scope) {
    scope.snapshot = undefined;
    for (const [listener, consumer] of scope.listeners) if (consumer.isActive()) listener();
  }

  restore(storageKey: string) {
    const scope = this.scope(storageKey);
    if (!scope.loaded && typeof window !== 'undefined') {
      const saved = loadPending(storageKey);
      scope.pending = saved.pending;
      scope.reloadProtected = saved.protected;
      scope.loaded = true;
      this.publish(scope);
    }
  }

  registerCompletion(key: string, kind: string, handler: Completion, consumer: Consumer) {
    const scope = this.scope(key);
    const handlers = scope.handlers.get(kind) ?? new Set<RegisteredCompletion>();
    const registration = { handler, consumer };
    handlers.add(registration);
    scope.handlers.set(kind, handlers);
    return () => {
      handlers.delete(registration);
      if (!handlers.size) scope.handlers.delete(kind);
    };
  }

  private clearPending(storageKey: string, scope: Scope) {
    scope.pending = null;
    try {
      window.sessionStorage.removeItem(storageKey);
      if (storageKey.toLowerCase() !== storageKey) window.sessionStorage.removeItem(storageKey.toLowerCase());
    } catch {
      // A retained storage entry safely replays the known receipt.
    }
  }

  clearError(key: string) {
    const scope = this.scope(key);
    scope.error = null;
    scope.conflict = false;
    scope.actionKind = null;
    this.publish(scope);
  }

  private async runMutation<T>(storageKey: string, mutation: FrozenMutation): Promise<T> {
    const scope = this.scope(storageKey);
    scope.inFlight = true;
    scope.pending = mutation;
    scope.error = null;
    scope.conflict = false;
    scope.actionKind = mutation.kind ?? null;
    try { window.sessionStorage.setItem(storageKey, JSON.stringify(mutation)); } catch {
      scope.reloadProtected = false;
    }
    this.publish(scope);
    try {
      const response = await api<T>(mutation.path, {
        method: mutation.method,
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': mutation.idempotencyKey },
        body: JSON.stringify(mutation.body),
      });
      // Layout cleanup retires ownership during the navigation commit. Passive
      // subscriptions/handlers may still exist until React's next effect flush.
      if (![...scope.listeners.values()].some(consumer => consumer.isActive())) {
        scope.pending = freeze({ ...mutation, uncertain: true });
        return response as T;
      }
      this.clearPending(storageKey, scope);
      for (const { handler, consumer } of [...(scope.handlers.get(mutation.kind ?? '') ?? []), ...(scope.handlers.get('*') ?? [])]) {
        if (!consumer.isActive()) continue;
        try { handler(response, mutation); } catch {
          scope.error = 'The action was saved, but its view could not refresh. Reload to read the saved result.';
        }
      }
      return response as T;
    } catch (err) {
      const rejected = err instanceof ApiError &&
        (err.status === 409 || err.status === 422 ||
          (!mutation.uncertain && err.status >= 400 && err.status < 500 && err.status !== 408 && err.status !== 429));
      if (rejected) {
        this.clearPending(storageKey, scope);
        scope.conflict = err.status === 409;
        scope.actionKind = mutation.kind ?? null;
        scope.error = err.status === 409
          ? mutation.kind === 'execution-command'
            ? 'Execution version conflict: The execution version changed concurrently.'
            : 'Epic version conflict: The epic version changed on the server before saving.'
          : err.status === 422
            ? Object.entries(err.fields).length
              ? 'Validation failed: ' + Object.entries(err.fields).map(([field, message]) => field + ': ' + message).join('; ')
              : 'Validation failed. Check the required fields.'
            : 'This action is unavailable or was rejected. Your edits are preserved.';
      } else {
        scope.pending = freeze({ ...mutation, uncertain: true });
        scope.actionKind = mutation.kind ?? null;
        try { window.sessionStorage.setItem(storageKey, JSON.stringify(scope.pending)); } catch {
          scope.reloadProtected = false;
        }
        scope.error = 'The action result is uncertain. Retry its original request before starting another.';
      }
      throw err;
    } finally {
      scope.inFlight = false;
      this.publish(scope);
    }
  }

  async execute<T>(
    storageKey: string, method: HttpMethod, path: string, body: Record<string, unknown>,
    options: { idempotencyKey?: string; kind?: string } = {},
  ): Promise<T> {
    this.restore(storageKey);
    const scope = this.scope(storageKey);
    if (scope.inFlight) throw new Error('A mutation is already in progress.');
    if (scope.pending) throw new Error('A competing mutation is unresolved. Retry the original request first.');
    return this.runMutation<T>(storageKey, freeze({
      kind: options.kind, method, path,
      body: JSON.parse(JSON.stringify(body)) as Record<string, unknown>,
      idempotencyKey: options.idempotencyKey ?? crypto.randomUUID(), timestamp: Date.now(),
    }));
  }

  async retryPending<T>(storageKey: string): Promise<T> {
    this.restore(storageKey);
    const scope = this.scope(storageKey);
    if (scope.inFlight) throw new Error('A mutation is already in progress.');
    if (!scope.pending) throw new Error('No pending mutation to retry.');
    return this.runMutation<T>(storageKey, freeze({ ...scope.pending, uncertain: true }));
  }
}

let browserStore: EpicMutationStore | null = null;

function getBrowserStore(): EpicMutationStore {
  if (!browserStore) {
    browserStore = new EpicMutationStore();
  }
  return browserStore;
}

export function resetEpicMutationStoreForTesting() {
  browserStore = null;
}

function useLocalEpicMutations(epicId?: string, enabled = true) {
  const canonicalEpicId = normalizeEpicId(epicId);
  const storageKey = 'epic_pending_mutation_' + (canonicalEpicId ?? 'create');
  const store = useMemo(() => (typeof window !== 'undefined' ? getBrowserStore() : new EpicMutationStore()), []);
  const activeScope = useRef<string | null>(null);
  const consumer = useMemo<Consumer>(() => ({ isActive: () => activeScope.current === storageKey }), [storageKey]);
  useLayoutEffect(() => {
    activeScope.current = storageKey;
    return () => { activeScope.current = null; };
  }, [storageKey]);
  const subscribe = useCallback((listener: () => void) => store.subscribe(storageKey, listener, consumer), [store, storageKey, consumer]);
  const getSnapshot = useCallback(() => store.snapshot(storageKey), [store, storageKey]);
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, () => INITIAL_SNAPSHOT);
  useEffect(() => { if (enabled) store.restore(storageKey); }, [enabled, store, storageKey]);
  const execute = useCallback(<T,>(method: HttpMethod, path: string, body: Record<string, unknown>, options: { idempotencyKey?: string; kind?: string } = {}) => store.execute<T>(storageKey, method, path, body, options), [store, storageKey]);
  const retryPending = useCallback(<T,>() => store.retryPending<T>(storageKey), [store, storageKey]);
  const clearError = useCallback(() => store.clearError(storageKey), [store, storageKey]);
  const registerCompletion = useCallback((kind: string, handler: Completion) => store.registerCompletion(storageKey, kind, handler, consumer), [store, storageKey, consumer]);
  return { ...snapshot, execute, retryPending, clearError, registerCompletion };
}

type MutationOwner = ReturnType<typeof useLocalEpicMutations>;
const EpicMutationContext = createContext<{ epicId: string; owner: MutationOwner } | null>(null);

export function EpicMutationProvider({ epicId, children }: { epicId: string; children: ReactNode }) {
  const owner = useLocalEpicMutations(epicId);
  return createElement(EpicMutationContext.Provider, { value: { epicId, owner } }, children);
}

export function useEpicMutations(epicId?: string): MutationOwner & { shared: boolean } {
  const shared = useContext(EpicMutationContext);
  const matching = shared !== null && normalizeEpicId(shared.epicId) === normalizeEpicId(epicId);
  const local = useLocalEpicMutations(epicId, !matching);
  return matching ? { ...shared.owner, registerCompletion: local.registerCompletion, shared: true } : { ...local, shared: false };
}
