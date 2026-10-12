import { describe, expect, test } from 'vitest';
import { retainedPolicyIntegersAreSafe } from './retained-policy-integers';

describe('retainedPolicyIntegersAreSafe', () => {
  test('accepts null and undefined fields', () => {
    expect(retainedPolicyIntegersAreSafe({})).toBe(true);
    expect(retainedPolicyIntegersAreSafe({
      hard_token_cap: null,
      hard_context_cap: null,
      manual_estimate_tokens: null,
      history_limit: null,
      minimum_comparables: null,
      window_days: null,
    })).toBe(true);
  });

  test('accepts safe integer numbers', () => {
    expect(retainedPolicyIntegersAreSafe({
      hard_token_cap: 5000,
      hard_context_cap: 10000,
      manual_estimate_tokens: 0,
      history_limit: 50,
      minimum_comparables: 20,
      window_days: 30,
    })).toBe(true);
  });

  test('accepts valid exact signed64 decimal strings up to INT64_MAX across boundaries', () => {
    expect(retainedPolicyIntegersAreSafe({
      hard_token_cap: '9007199254740992', // MAX_SAFE + 1
      hard_context_cap: '9007199254740993', // MAX_SAFE + 2
      manual_estimate_tokens: '9223372036854775807', // INT64_MAX
      history_limit: '9007199254740992',
      minimum_comparables: '9007199254740992',
      window_days: '9223372036854775807',
    })).toBe(true);
  });

  test('rejects unsafe JavaScript numbers exceeding MAX_SAFE_INTEGER', () => {
    expect(retainedPolicyIntegersAreSafe({
      hard_token_cap: Number.MAX_SAFE_INTEGER + 1,
    })).toBe(false);
    expect(retainedPolicyIntegersAreSafe({
      manual_estimate_tokens: Number.MAX_SAFE_INTEGER + 1,
    })).toBe(false);
  });

  test('rejects overflow beyond INT64_MAX', () => {
    expect(retainedPolicyIntegersAreSafe({
      hard_token_cap: '9223372036854775808',
    })).toBe(false);
    expect(retainedPolicyIntegersAreSafe({
      history_limit: '9223372036854775808',
    })).toBe(false);
  });

  test('rejects negative, fractional, and non-numeric values', () => {
    expect(retainedPolicyIntegersAreSafe({ hard_token_cap: -1 })).toBe(false);
    expect(retainedPolicyIntegersAreSafe({ hard_token_cap: '-1' })).toBe(false);
    expect(retainedPolicyIntegersAreSafe({ hard_token_cap: 1.5 })).toBe(false);
    expect(retainedPolicyIntegersAreSafe({ hard_token_cap: '1.5' })).toBe(false);
    expect(retainedPolicyIntegersAreSafe({ hard_token_cap: 'abc' })).toBe(false);
  });
});
