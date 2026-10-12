import { describe, expect, test } from 'vitest';
import { knownReportTotal, nextCheckpointThreshold, retainedPolicyIntegersAreSafe } from './retained-policy-integers';
import { parseExactJson } from '@/lib/api/exact-integer';

describe('next checkpoint headroom', () => {
  test.each([
    ['9223372036854775807', null, null],
    ['9223372036854775806', null, '9223372036854775807'],
    ['9223372036854775807', 0, null],
    ['9223372036854775787', 20, '9223372036854775807'],
    ['9223372036854775788', 20, null],
    ['9223372036854775805', 1.1, '9223372036854775807'],
    ['9223372036854775806', 1.1, null],
    ['9007199254740992', '2e0', '9007199254740994'],
  ] as const)('measured %s plus frozen reference %s gives %s', (measured, reference, expected) => {
    expect(nextCheckpointThreshold(BigInt(measured), reference)?.toString() ?? null).toBe(expected);
  });

  test('keeps MAX known, distinguishes unknown/invalid/overflow reports, and refuses invalid reference', () => {
    expect(knownReportTotal({ completeness: 'complete', input_tokens: '9223372036854775807', output_tokens: 0 })).toBe(9223372036854775807n);
    expect(knownReportTotal({ completeness: 'lower_bound', input_tokens: '9007199254740992', output_tokens: null })).toBe(9007199254740992n);
    expect(knownReportTotal({ completeness: 'lower_bound', input_tokens: null, output_tokens: null })).toBeNull();
    expect(knownReportTotal({ completeness: 'complete', input_tokens: 'invalid', output_tokens: 5 })).toBeNull();
    expect(knownReportTotal({ completeness: 'complete', input_tokens: '9223372036854775807', output_tokens: 1 })).toBeNull();
    expect(nextCheckpointThreshold(null, 1)).toBeNull();
    expect(nextCheckpointThreshold(0n, 'not-a-number')).toBeNull();
    expect(nextCheckpointThreshold(0n, Number.POSITIVE_INFINITY)).toBeNull();
  });

  test('uses floating frozen-reference semantics after exponent-form API parsing', () => {
    const parsed = parseExactJson('{"reference_tokens":9.007199254740992e15}') as { reference_tokens: string };
    expect(parsed.reference_tokens).toBe('9007199254740992');
    expect(nextCheckpointThreshold(0n, parsed.reference_tokens)).toBe(9007199254740992n);
    expect(nextCheckpointThreshold(9223372036854775806n, 1.1)).toBeNull();
  });
});

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
