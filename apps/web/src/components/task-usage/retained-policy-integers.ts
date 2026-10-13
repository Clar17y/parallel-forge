import { MAX_SIGNED_64 } from '@/lib/api/exact-integer';

export type RetainedPolicyIntegers = {
  manual_estimate_tokens?: number | string | null;
  hard_token_cap?: number | string | null;
  hard_context_cap?: number | string | null;
  history_limit?: number | string | null;
  minimum_comparables?: number | string | null;
  window_days?: number | string | null;
};

export type UsageReportDimensions = {
  input_tokens?: number | string | null;
  output_tokens?: number | string | null;
  completeness?: string | null;
};

export function isSafePolicyInteger(value: number | string | null | undefined): boolean {
  if (value == null) return true;
  if (typeof value === 'number') {
    return Number.isSafeInteger(value) && value >= 0;
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!/^\d+$/.test(trimmed)) return false;
    try {
      const big = BigInt(trimmed);
      return big >= 0n && big <= MAX_SIGNED_64;
    } catch {
      return false;
    }
  }
  return false;
}

export function retainedPolicyIntegersAreSafe(policy: RetainedPolicyIntegers): boolean {
  return [
    policy.manual_estimate_tokens,
    policy.hard_token_cap,
    policy.hard_context_cap,
    policy.history_limit,
    policy.minimum_comparables,
    policy.window_days,
  ].every(isSafePolicyInteger);
}

export function parseDimension(value: number | string | null | undefined): bigint | 'unknown' | 'invalid' {
  if (value == null) return 'unknown';
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value) || value < 0) return 'invalid';
    return BigInt(value);
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!/^\d+$/.test(trimmed)) return 'invalid';
    try {
      const big = BigInt(trimmed);
      if (big < 0n || big > MAX_SIGNED_64) return 'invalid';
      return big;
    } catch {
      return 'invalid';
    }
  }
  return 'invalid';
}

export function knownReportTotal(report: UsageReportDimensions | null | undefined): bigint | null {
  if (!report || !['complete', 'lower_bound'].includes(report.completeness ?? '')) return null;
  const dims = [report.input_tokens, report.output_tokens].map(parseDimension);
  if (dims.some(d => d === 'invalid')) return null;
  const validDims = dims.filter((d): d is bigint => typeof d === 'bigint');
  if (!validDims.length) return null;
  const total = validDims.reduce((sum, d) => sum + d, 0n);
  if (total > MAX_SIGNED_64) return null;
  return total;
}

/** Match the API's max(1, ceil(frozen reference)) without rounding measured integers. */
export function nextCheckpointThreshold(total: bigint | null, referenceTokens: number | string | null | undefined): bigint | null {
  if (total === null || total < 0n || total > MAX_SIGNED_64) return null;
  if (typeof referenceTokens === 'string' && !/^(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(referenceTokens)) return null;
  const reference = referenceTokens == null ? 0 : Number(referenceTokens);
  if (!Number.isFinite(reference) || reference < 0) return null;
  const step = reference === 0 ? 1n : BigInt(Math.ceil(reference));
  const threshold = total + (step > 1n ? step : 1n);
  return threshold <= MAX_SIGNED_64 ? threshold : null;
}
