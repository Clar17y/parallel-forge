import { expect, test } from 'vitest';
import { getModelReasoningSupport, validateReasoningSetting } from './api-reasoning';

test.each([
  ['Google', 'gemini-3.5-flash'],
  ['google', ' Gemini-3.5-flash'],
  ['google', 'gemini-3.5-flash '],
  ['google', 'gemini-3.5-flash\n'],
  ['google\n', 'gemini-3.5-flash'],
])('uses exact backend model identity for %s/%s', (provider, model) => {
  expect(getModelReasoningSupport(provider, model).supported).toBe(false);
  expect(validateReasoningSetting(provider, model, 'low')).toContain('not supported');
});

test('legacy automatic settings remain valid for custom identities', () => {
  expect(validateReasoningSetting('custom-provider', 'custom-model', null)).toBeNull();
});
