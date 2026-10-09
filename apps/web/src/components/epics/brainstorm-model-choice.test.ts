import { expect, test } from 'vitest';
import { isLocalBrainstormRoute } from './brainstorm-model-choice';

const route = { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-luna', auth_mode: 'subscription', billing_mode: 'allowance_only' };

test('accepts the domain effort values and rejects provider-native values', () => {
  for (const effort of ['none', 'low', 'medium', 'high', 'maximum']) {
    expect(isLocalBrainstormRoute({ ...route, effort })).toBe(true);
  }
  for (const effort of ['minimal', 'xhigh', 'max', 'ultra']) {
    expect(isLocalBrainstormRoute({ ...route, effort })).toBe(false);
  }
});

test.each([
  { effort: ['maximum'] },
  { provider: 'google', client: ['gemini_cli'], effort: 'medium' },
  { model: '   ', effort: 'medium' },
])('rejects malformed saved choices without coercing route fields: %j', value => {
  expect(isLocalBrainstormRoute({ ...route, ...value })).toBe(false);
});
