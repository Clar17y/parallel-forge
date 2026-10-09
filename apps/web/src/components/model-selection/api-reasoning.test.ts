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

test.each([
  ['custom-provider', 'gemini-3.8-flash-medium'],
  ['google', ' gemini-3.8-flash-medium'],
  ['google', 'gemini-3.8-flash-medium '],
  ['google', 'gemini-3.8-flash-medium\n'],
])('preserves automatic custom identities exactly for %s/%s', (provider, model) => {
  expect(validateReasoningSetting(provider, model, null)).toBeNull();
});

test.each([
  'gemini-3.8-flash-low',
  'gemini-3.8-flash-medium',
  'gemini-3.8-flash-high',
  'gemini-3.8-flash-max',
  'gemini-3.8-flash-xhigh',
  'gemini-3.8-flash-none',
  'gemini-2.5-flash-medium',
])('rejects known Gemini CLI composite identity %s even in automatic mode', model => {
  const support = getModelReasoningSupport('google', model);
  expect(support.supported).toBe(false);
  expect(support.unsupportedReason).toContain('CLI composite identity');
  expect(support.unsupportedReason).toContain('separately selected reasoning');

  const errNone = validateReasoningSetting('google', model, null);
  expect(errNone).toContain('CLI composite identity');
  expect(errNone).toContain('separately selected reasoning');

  const errExplicit = validateReasoningSetting('google', model, 'medium');
  expect(errExplicit).toContain('CLI composite identity');
});

test.each([
  'gemini-3.1-pro',
  'gemini-3-pro',
  'gemini-3.0-pro',
  'gemini-3.8-flash-latest',
  'gemini-3.8-flash-madeup',
  'gemini-3.8-flash-preview',
])('rejects non-allowlist models for explicit reasoning: %s', model => {
  expect(getModelReasoningSupport('google', model).supported).toBe(false);
  expect(validateReasoningSetting('google', model, 'low')).toContain('not supported');
});

test.each([
  'gemini-2.5-flash',
  'gemini-2.5-pro',
  'gemini-3.5-flash',
  'gemini-3.8-flash',
  'gemini-3.1-pro-preview',
  'gemini-3.1-pro-preview-customtools',
])('supports exact allowlist model %s for low/medium/high', model => {
  const support = getModelReasoningSupport('google', model);
  expect(support.supported).toBe(true);
  expect(support.supportedEfforts).toEqual(['low', 'medium', 'high']);
  expect(validateReasoningSetting('google', model, 'low')).toBeNull();
  expect(validateReasoningSetting('google', model, 'medium')).toBeNull();
  expect(validateReasoningSetting('google', model, 'high')).toBeNull();
});
