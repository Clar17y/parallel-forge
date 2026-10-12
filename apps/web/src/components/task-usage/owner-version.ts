import { MAX_SIGNED_64 } from '@/lib/api/exact-integer';

export type ExactVersion = number | string;

export function versionValue(value: ExactVersion | null | undefined, max = MAX_SIGNED_64): bigint | null {
  if (typeof value === 'number') {
    return Number.isSafeInteger(value) && value >= 0 && BigInt(value) <= max ? BigInt(value) : null;
  }
  if (typeof value !== 'string' || !/^(0|[1-9][0-9]*)$/.test(value)) return null;
  const exact = BigInt(value);
  return exact <= max ? exact : null;
}

export function nextVersion(value: ExactVersion | null | undefined): ExactVersion | null {
  const current = versionValue(value, MAX_SIGNED_64 - 1n);
  if (current === null) return null;
  const next = current + 1n;
  return next <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(next) : next.toString();
}

export function matchesOwnerReceipt(
  selected: { checkpoint_id: string; action: string; expected_version: ExactVersion },
  result: { checkpoint: { checkpoint_id: string; version: ExactVersion };
    receipt: { checkpoint_id: string; action: string; result_version: ExactVersion } } | null | undefined,
): boolean {
  if (!result || result.receipt.checkpoint_id !== selected.checkpoint_id
    || result.receipt.action !== selected.action || result.checkpoint.checkpoint_id !== selected.checkpoint_id) return false;
  const expected = versionValue(selected.expected_version, MAX_SIGNED_64 - 1n);
  const receipt = versionValue(result.receipt.result_version);
  const checkpoint = versionValue(result.checkpoint.version);
  return expected !== null && receipt !== null && checkpoint !== null
    && receipt >= expected + 1n && receipt <= checkpoint;
}
