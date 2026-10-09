export type BrainstormRoute = {
  provider: string;
  client: string;
  model: string;
  effort: 'none' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max' | 'ultra';
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
    (route.provider === 'google' && ['gemini_cli', 'antigravity_cli'].includes(String(route.client))) ||
    (route.provider === 'anthropic' && route.client === 'claude_code')) &&
    typeof route.model === 'string' && route.model.length > 0 && route.model.length <= 255 &&
    ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'].includes(String(route.effort)) &&
    route.auth_mode === 'subscription' && route.billing_mode === 'allowance_only';
}

export function toBrainstormRoute(value: unknown): BrainstormRoute | null {
  if (!isLocalBrainstormRoute(value)) return null;
  return { provider: value.provider, client: value.client, model: value.model, effort: value.effort,
    auth_mode: 'subscription', billing_mode: 'allowance_only' };
}
