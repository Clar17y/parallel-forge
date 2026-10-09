import type { components } from '@/lib/api/schema';

export type BrainstormRoute = {
  provider: string;
  client: string;
  model: string;
  effort: components['schemas']['ReasoningEffort'];
  auth_mode: 'subscription';
  billing_mode: 'allowance_only';
};

export function routeKey(route: BrainstormRoute): string {
  return JSON.stringify([route.provider, route.client, route.model, route.effort]);
}

export function isLocalBrainstormRoute(value: unknown): value is BrainstormRoute {
  if (!value || typeof value !== 'object') return false;
  const route = value as Record<string, unknown>;
  return ((route.provider === 'openai' && route.client === 'codex_app_server') ||
    (route.provider === 'google' && (route.client === 'gemini_cli' || route.client === 'antigravity_cli')) ||
    (route.provider === 'anthropic' && route.client === 'claude_code')) &&
    typeof route.model === 'string' && route.model.trim().length > 0 && route.model.length <= 255 &&
    typeof route.effort === 'string' && ['none', 'low', 'medium', 'high', 'maximum'].includes(route.effort) &&
    route.auth_mode === 'subscription' && route.billing_mode === 'allowance_only';
}

export function toBrainstormRoute(value: unknown): BrainstormRoute | null {
  if (!isLocalBrainstormRoute(value)) return null;
  return { provider: value.provider, client: value.client, model: value.model, effort: value.effort,
    auth_mode: 'subscription', billing_mode: 'allowance_only' };
}

type Runtime = components['schemas']['SubscriptionRuntimeStatusPage'];

export function runtimeFreshMs(runtime: Runtime | undefined): number {
  const seconds = runtime?.fresh_for_seconds;
  return Number.isFinite(seconds) && seconds !== undefined && seconds > 0
    ? Math.min(seconds, 45) * 1000
    : 45_000;
}

export function availableBrainstormRoutes(runtime: Runtime | undefined, startedAt: number, now: number): {
  routes: BrainstormRoute[];
  expiresAt: number | null;
} {
  if (!runtime) return { routes: [], expiresAt: null };
  const freshMs = runtimeFreshMs(runtime);
  const observedAt = Date.parse(runtime.observed_at);
  const pageExpiry = startedAt + freshMs;
  if (now >= pageExpiry) return { routes: [], expiresAt: null };
  const routes: BrainstormRoute[] = [];
  let expiresAt: number | null = null;
  for (const worker of runtime.workers ?? []) {
    if (worker.state !== 'current') continue;
    const lastSeenAt = Date.parse(worker.last_seen_at);
    const observedAge = Number.isFinite(observedAt) && Number.isFinite(lastSeenAt) && observedAt >= lastSeenAt
      ? observedAt - lastSeenAt
      : 0;
    const workerExpiry = Math.min(pageExpiry, startedAt + Math.max(0, freshMs - observedAge));
    if (now >= workerExpiry) continue;
    for (const candidate of worker.routes ?? []) {
      if (candidate.configured === false || candidate.admitted === false || candidate.effective_reason === 'stale_worker') continue;
      const route = toBrainstormRoute(candidate);
      if (route) routes.push(route);
    }
    if (expiresAt === null || workerExpiry < expiresAt) expiresAt = workerExpiry;
  }
  return { routes, expiresAt };
}
