import { expect, test } from 'vitest';
import { routePresetIdentity, routePresets, withCurrentRoute } from './model-presets';
import type { Route, SubscriptionModelCatalogView } from '../subscription-profiles/models';

const catalog = (provider: string, client: string, efforts: Route['effort'][]): SubscriptionModelCatalogView => ({
  provider, client, source: 'provider', status: 'available', observed_at: '2026-10-01T00:00:00Z', stale: false,
  models: [{ id: 'same-model', label: 'Same model', efforts }], message: '',
});

test('groups effort levels under one model while distinguishing provider and client collisions', () => {
  const presets = routePresets([catalog('openai', 'codex', ['low', 'maximum']), catalog('google', 'gemini', ['low'])]);
  const openai = presets.find(item => item.provider === 'openai' && item.model === 'same-model')!;
  const google = presets.find(item => item.provider === 'google' && item.model === 'same-model')!;
  expect(presets.filter(item => item.model === 'same-model')).toHaveLength(2);
  expect(openai.efforts).toEqual(['low', 'maximum']);
  expect(new Set([routePresetIdentity(openai), routePresetIdentity(google)]).size).toBe(2);
  const providerCollision = routePresets([catalog('openai', 'shared', ['low']), catalog('google', 'shared', ['low'])])
    .filter(item => item.model === 'same-model');
  expect(new Set(providerCollision.map(item => item.label)).size).toBe(2);
});

test('retains uncataloged saved effort and keeps friendly offline suggestions advisory', () => {
  const route = { provider: 'openai', client: 'codex', model: 'custom', effort: 'maximum', auth_mode: 'api_key', billing_mode: 'paid_opt_in' } as Route;
  const current = withCurrentRoute(routePresets(), route);
  expect(current.some(item => item.model === 'custom' && item.efforts.includes('maximum'))).toBe(true);
  expect(current.find(item => item.model === 'gpt-6.1-sol')?.source).toBe('suggestion');
  const gemini = current.find(item => item.model === 'gemini-3.8-flash');
  expect(gemini?.source).toBe('suggestion');
  expect(gemini?.defaultEffort).toBe('medium');
});

test('keeps an unsupported saved effort alongside restricted catalog presets until changed', () => {
  const restricted = catalog('google', 'gemini', ['low']);
  const saved = { provider: 'google', client: 'gemini', model: 'same-model', effort: 'high', auth_mode: 'api_key', billing_mode: 'paid_opt_in' } as Route;
  const choices = withCurrentRoute(routePresets([restricted]), saved);
  expect(choices.some(choice => routePresetIdentity(choice) === routePresetIdentity(saved))).toBe(true);
  expect(choices.some(choice => choice.model === 'same-model' && choice.efforts[0] === 'low' && choice.source === 'catalog')).toBe(true);
});

test('catalog effort declarations replace broader offline suggestion levels for the same tuple', () => {
  const restricted = catalog('openai', 'codex_app_server', ['low']);
  restricted.models[0].id = 'gpt-6.1-sol';
  const suggestion = routePresets([restricted]).find(item => item.model === 'gpt-6.1-sol')!;
  expect(suggestion.source).toBe('catalog');
  expect(suggestion.efforts).toEqual(['low']);
});
