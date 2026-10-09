import { expect, test } from 'vitest';
import { availableBrainstormRoutes, isLocalBrainstormRoute } from './brainstorm-model-choice';

const route = { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-luna', auth_mode: 'subscription', billing_mode: 'allowance_only' };

test('accepts the domain effort values and rejects provider-native values', () => {
  for (const effort of ['none', 'low', 'medium', 'high', 'maximum']) {
    expect(isLocalBrainstormRoute({ ...route, effort })).toBe(true);
  }
  for (const effort of ['minimal', 'xhigh', 'max', 'ultra']) {
    expect(isLocalBrainstormRoute({ ...route, effort })).toBe(false);
  }
});

test('expires current worker routes from last report age and keeps a live duplicate', () => {
  const observedAt = Date.parse('2026-10-09T12:00:00Z');
  const available = {
    observed_at: '2026-10-09T12:00:00Z', fresh_for_seconds: 45,
    workers: [
      { state: 'current', last_seen_at: '2026-10-09T11:59:16Z', routes: [{ ...route, effort: 'low', configured: true, admitted: true }] },
      { state: 'stale', last_seen_at: '2026-10-09T11:58:00Z', routes: [{ ...route, effort: 'medium', configured: true, admitted: true }] },
      { state: 'current', last_seen_at: '2026-10-09T12:00:00Z', routes: [{ ...route, effort: 'high', configured: true, admitted: true }] },
    ],
  } as unknown as Parameters<typeof availableBrainstormRoutes>[0];
  expect(availableBrainstormRoutes(available, observedAt, observedAt).routes.map(item => item.effort)).toEqual(['low', 'high']);
  expect(availableBrainstormRoutes(available, observedAt, observedAt + 1_001).routes.map(item => item.effort)).toEqual(['high']);
  expect(availableBrainstormRoutes(available, observedAt, observedAt + 45_000).routes).toEqual([]);
});

test('unknown freshness timestamps have a bounded fallback and never disable the default route', () => {
  const available = { workers: [{ state: 'current', routes: [{ ...route, effort: 'low', admitted: true }] }] } as unknown as Parameters<typeof availableBrainstormRoutes>[0];
  expect(availableBrainstormRoutes(available, 1000, 1000).routes).toHaveLength(1);
  expect(availableBrainstormRoutes(available, 1000, 46_000).routes).toHaveLength(0);
});

test.each([
  { effort: ['maximum'] },
  { provider: 'google', client: ['gemini_cli'], effort: 'medium' },
  { model: '   ', effort: 'medium' },
])('rejects malformed saved choices without coercing route fields: %j', value => {
  expect(isLocalBrainstormRoute({ ...route, ...value })).toBe(false);
});
