import { expect, test } from 'vitest';
import { matchesOwnerReceipt, nextVersion, versionValue } from './owner-version';

test('validates exact signed64 boundaries without accepting rounded numbers', () => {
  expect(versionValue(Number.MAX_SAFE_INTEGER)).toBe(9007199254740991n);
  expect(versionValue('9007199254740992')).toBe(9007199254740992n);
  expect(versionValue('9223372036854775807')).toBe(9223372036854775807n);
  expect(versionValue(Number.MAX_SAFE_INTEGER + 2)).toBeNull();
  expect(versionValue('09007199254740993')).toBeNull();
  expect(versionValue('9223372036854775808')).toBeNull();
  expect(nextVersion('9223372036854775806')).toBe('9223372036854775807');
  expect(nextVersion('9223372036854775807')).toBeNull();
});

test('accepts an older exact durable replay and rejects mismatched or impossible receipts', () => {
  const selected = { checkpoint_id: 'checkpoint-1', action: 'continue', expected_version: '9007199254740992' };
  const result = { checkpoint: { checkpoint_id: 'checkpoint-1', version: '9007199254740994' },
    receipt: { checkpoint_id: 'checkpoint-1', action: 'continue', result_version: '9007199254740993' } };
  expect(matchesOwnerReceipt(selected, result)).toBe(true);
  expect(matchesOwnerReceipt(selected, { ...result, receipt: { ...result.receipt, result_version: '9007199254740992' } })).toBe(false);
  expect(matchesOwnerReceipt(selected, { ...result, receipt: { ...result.receipt, result_version: '9007199254740995' } })).toBe(false);
  expect(matchesOwnerReceipt(selected, { ...result, receipt: { ...result.receipt, checkpoint_id: 'other' } })).toBe(false);
  expect(matchesOwnerReceipt(selected, { ...result, receipt: { ...result.receipt, action: 'stop' } })).toBe(false);
  expect(matchesOwnerReceipt(selected, { ...result, receipt: { ...result.receipt, result_version: 9007199254740992 } })).toBe(false);
});
